"""저장 완료 알림까지 확인한 후 ack한다. 최종 확정/사용자 조회 권한은 백엔드 책임이다."""

import json
from pathlib import Path
from uuid import uuid4

from openai import APIConnectionError, APIStatusError
from pydantic import SecretStr, TypeAdapter, ValidationError

from ..evaluation_agent.final.validation import EvaluationError
from ..integrations.final_mcp import FinalMCPGateway
from ..integrations.final_tokens import FinalTokenError
from ..integrations.mcp_client import MCPError
from ..observability import TraceSession
from ..shared.final_job_contracts import (
    FINAL_QUEUE_CONTRACTS,
    BackendEvaluationEvent,
    BackendFinalEvaluationEvent,
    FinalEvaluationEvent,
    FinalEvaluationSaved,
)
from ..shared.live_feedback_contracts import ID
from .final_evaluation import FinalEvaluationJob, FinalEvaluationWorkflow

_ID = TypeAdapter(ID)
MAX_EVENT_BYTES = 16384
REQUEST_QUEUE = "final_evaluation.requests"
RETRY_QUEUE = "final_evaluation.retry"
DLQ = "final_evaluation.dlq"
RESULT_QUEUE = "final_evaluation.results"


class LifecycleEventRequiresBackend(ValueError):
    """RESUME은 평가 오류가 아니다. ack/DLQ 없이 소비를 중단하고 서버 처리를 기다린다."""

    def __init__(self):
        super().__init__("RESUME_REQUIRES_BACKEND")


def failure_code(exc):
    """오류 문장/서버의 임의 code는 발행하지 않고 알려진 원인만 분류한다."""
    if isinstance(exc, BaseExceptionGroup):
        codes = [failure_code(child) for child in exc.exceptions]
        if all(retry for _, retry in codes):
            return "DEPENDENCY_UNAVAILABLE", True
        return "EXECUTION_FAILED", False
    if isinstance(exc, FinalTokenError):
        return exc.code, exc.retryable
    if isinstance(exc, MCPError):
        retry = exc.code in {"CONNECTION_FAILED", "TRANSPORT_ERROR"}
        return ("MCP_UNAVAILABLE", True) if retry else ("MCP_REJECTED", False)
    if isinstance(exc, APIConnectionError):
        return "MODEL_UNAVAILABLE", True
    if isinstance(exc, APIStatusError):
        if exc.status_code in {408, 429} or exc.status_code >= 500:
            return "MODEL_UNAVAILABLE", True
        return "MODEL_REJECTED", False
    # 평가기는 모델 전송 오류를 EvaluationError의 명시적 원인으로 보존한다.
    if isinstance(exc, EvaluationError):
        if isinstance(exc.__cause__, (APIConnectionError, APIStatusError)):
            return failure_code(exc.__cause__)
        return "EVALUATION_REJECTED", False
    if isinstance(exc, ValidationError):
        return "INVALID_RESULT", False
    return "EXECUTION_FAILED", False


def _evaluation_id(body: bytes) -> str | None:
    if len(body) > MAX_EVENT_BYTES:
        return None
    try:
        return _ID.validate_python(json.loads(body).get("evaluation_id"))
    except (ValueError, AttributeError, TypeError, RecursionError):
        return None


def saved_notification(event, result):
    if result.get("status") != "draft_and_coaching_saved":
        raise ValueError("incomplete save")
    if result.get("evaluation_id") != event.evaluation_id:
        raise ValueError("wrong evaluation")
    revisions = {}
    for tool, field in (
        ("save_evaluation_draft", "draft_revision"),
        ("save_coaching", "coaching_revision"),
    ):
        receipt = result["receipts"][tool]
        if receipt.get("evaluation_id") != event.evaluation_id:
            raise ValueError("wrong receipt")
        revisions[field] = receipt[field]
    job = FinalEvaluationJob(
        evaluation_id=event.evaluation_id, operation_id=event.operation_id,
    )
    # 재전달마다 같은 완료 ID. 백엔드도 이 ID로 중복 처리를 막아야 한다.
    return FinalEvaluationSaved(
        message_id=f"final-saved:{job.key()}",
        evaluation_id=event.evaluation_id,
        operation_id=event.operation_id,
        review_required=result["review_required"],
        **revisions,
    )


class FinalJobExecutor:
    """신뢰하는 호출부가 작업마다 발급/복구한 토큰을 주입한다. 직접 DB/API 호출 없음."""

    def __init__(
        self, token_provider, client, *, model, business_url, rag_url,
        checkpoint_dir, code_provenance,
    ):
        self.token_provider, self.client = token_provider, client
        self.model, self.business_url, self.rag_url = model, business_url, rag_url
        self.checkpoint_dir, self.code_provenance = checkpoint_dir, code_provenance

    async def __call__(self, event):
        token = await self.token_provider(event)
        if not isinstance(token, SecretStr) or not token.get_secret_value().strip():
            raise ValueError("token provider must return a nonempty SecretStr")
        async with FinalMCPGateway(
            token.get_secret_value(), business_url=self.business_url, rag_url=self.rag_url,
        ) as gateway:
            workflow = FinalEvaluationWorkflow(
                gateway, self.client, model=self.model, checkpoint_dir=self.checkpoint_dir,
                code_provenance=self.code_provenance,
            )
            job = FinalEvaluationJob(evaluation_id=event.evaluation_id,
                                     operation_id=event.operation_id)
            if isinstance(event, BackendFinalEvaluationEvent):
                return await workflow.run(job, expected=event.backend_scope)
            return await workflow.run(job)


class FinalEvaluationWorker:
    def __init__(
        self, execute, *, trace_dir: Path, model: str, code_provenance: dict,
        langsmith=False, max_retries=3, contract="agent-v1",
    ):
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max retries must be 0..10")
        self.execute, self.trace_dir, self.model = execute, trace_dir, model
        self.code_provenance, self.langsmith = code_provenance, langsmith
        self.max_retries = max_retries
        self.queues = FINAL_QUEUE_CONTRACTS[contract]

    async def handle(self, body: bytes, *, publish, ack):
        try:
            if len(body) > MAX_EVENT_BYTES:
                raise ValueError("oversized event")
            wire = json.loads(body)
            if self.queues.name == "backend-v1":
                if isinstance(wire, dict) and wire.get("type") == "RESUME":
                    raise LifecycleEventRequiresBackend()
                source = BackendEvaluationEvent.model_validate(wire)
                event = source.normalized()
            else:
                source = event = FinalEvaluationEvent.model_validate(wire)
        except LifecycleEventRequiresBackend:
            raise
        except (ValidationError, ValueError, RecursionError):
            # RecursionError: 깊게 중첩된 JSON. 밖으로 내보내면 nack·재전달이 끝없이 반복된다
            # 원본은 DLQ에 복사하지 않는다. 평가 건 ID만 읽히면 백엔드가 처리 실패로 기록할 수
            # 있게 남긴다(없으면 백엔드가 버려 평가 건이 '평가 중'에 남는다).
            diagnostic = {"message_id": str(uuid4()), "type": "INVALID_FINAL_EVALUATE"}
            if evaluation_id := _evaluation_id(body):
                diagnostic["evaluation_id"] = evaluation_id
            await publish(self.queues.dlq, diagnostic, "INVALID_EVENT")
            await ack()
            return {"status": "dlq", "code": "INVALID_EVENT"}

        with TraceSession(
            self.trace_dir, langsmith=self.langsmith, metadata={
                "request_id": event.evaluation_id, "purpose": "final", "model": self.model,
                "git_commit": self.code_provenance.get("git_commit", "unknown"),
            },
        ) as trace:
            with trace.span("final.worker", inputs=event) as span:
                notification, failure = None, None
                if event.attempt > self.max_retries:
                    failure = ("RETRY_LIMIT", False)
                else:
                    try:
                        result = await self.execute(event)
                        notification = saved_notification(event, result)
                    except Exception as exc:
                        failure = failure_code(exc)
                if failure:
                    code, retryable = failure
                    retry = retryable and event.attempt < self.max_retries
                    # 백엔드 retry/DLQ 소비자가 읽을 수 있게 원래 EVALUATE 계약을 유지한다.
                    payload = source.model_dump(mode="json")
                    if retry:
                        payload["attempt"] += 1
                    await publish(self.queues.retry if retry else self.queues.dlq, payload, code)
                    outcome = {"status": "retry" if retry else "dlq", "code": code}
                else:
                    # 발행 실패는 업무 오류로 바꾸지 않는다. ack 없이 원본이 재전달된다.
                    await publish(
                        self.queues.results, notification.model_dump(mode="json"), "SAVED",
                    )
                    outcome = {"status": "draft_and_coaching_saved"}
                outcome.update(evaluation_id=event.evaluation_id, trace_id=trace.trace_id)
                span.result(outcome)
                span.outputs["status"] = outcome["status"]
                if failure:
                    span.outputs["error_code"] = failure[0]
            # 추적 종료 기록까지 성공한 후 ack. LangSmith 전송 실패는 로컬 실행에 영향 없음.
            await ack()
            return outcome
