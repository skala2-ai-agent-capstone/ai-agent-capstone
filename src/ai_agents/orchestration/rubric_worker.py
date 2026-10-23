"""내규 업로드 뒤 평가 기준 초안 생성 Worker.

RabbitMQ와 독립적으로 검증할 수 있는 수신·생성·저장·재시도·DLQ·완료 처리다.
업무 유형마다 현행 내규(요청에 고정한 버전)를 읽어 기준을 만들고 검사한 뒤
save_rubric으로 초안을 저장한다. 초안은 관리자가 승인해야 현행이 된다.
모두 저장하면 rubric_generation.results로 완료를 알리고, 재시도 한도를 넘기거나
처리할 수 없는 요청은 rubric_generation.dlq로 보낸다(백엔드가 실패로 기록).
"""

import hashlib
import json
import re
from uuid import uuid4

from openai import APIError
from pydantic import ValidationError

from ..criteria_agent.agent import generate_mcp_criteria
from ..criteria_agent.builder import RubricError
from ..integrations.mcp_client import MCPError
from ..observability import TraceSession
from ..shared.execution_contracts import DocumentRef
from ..shared.rubric_generation_contracts import (
    RubricGenerationEvent,
    RubricGenerationResult,
    SavedDraft,
)

REQUEST_QUEUE = "rubric_generation.requests"
RETRY_QUEUE = "rubric_generation.retry"
DLQ = "rubric_generation.dlq"
RESULTS_QUEUE = "rubric_generation.results"

# MCP 오류 코드 → (Worker 오류 코드, 재시도 여부). 없는 코드는 MCP_REJECTED로 멈춘다.
MCP_ERRORS = {
    "POLICY_INCOMPLETE": ("POLICY_INCOMPLETE", False),
    "REFERENCE_MISMATCH": ("POLICY_INCOMPLETE", False),
    # 업로드 직후 rag-mcp 사본이 아직 이전 버전이면 난다. 잠시 뒤 다시 읽는다.
    "VERSION_CONFLICT": ("VERSION_CONFLICT", True),
    "TEMPORARY_FAILURE": ("MCP_UNAVAILABLE", True),
    "TRANSPORT_ERROR": ("MCP_UNAVAILABLE", True),
    "CONNECTION_FAILED": ("MCP_UNAVAILABLE", True),
    # save_rubric 검사(현행 내규 조항·민감정보 등)에 걸림. 모델 출력이 매번 달라 다시 만든다.
    "VALIDATION_ERROR": ("RUBRIC_INVALID", True),
    "FORBIDDEN": ("TOKEN_REJECTED", False),
}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class RubricGenerationError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def classify(exc: Exception) -> RubricGenerationError:
    """생성·저장 중 예외를 Worker 오류 코드로 바꾼다. 원문 메시지는 내보내지 않는다."""
    if isinstance(exc, RubricGenerationError):
        return exc
    if isinstance(exc, MCPError):
        code, retryable = MCP_ERRORS.get(exc.code, ("MCP_REJECTED", False))
        return RubricGenerationError(code, retryable=retryable)
    if isinstance(exc, RubricError):
        if isinstance(exc.__cause__, APIError):
            return RubricGenerationError("LLM_UNAVAILABLE", retryable=True)
        return RubricGenerationError("RUBRIC_INVALID", retryable=True)
    if isinstance(exc, ValidationError):
        return RubricGenerationError("RUBRIC_INVALID", retryable=True)
    return RubricGenerationError("UNEXPECTED_ERROR")


def idempotency_key(event: RubricGenerationEvent, rubric: dict) -> str:
    """같은 요청·같은 내용이면 같은 키(재전달 때 MCP가 이전 저장 결과를 돌려준다).
    다시 생성해 내용이 달라지면 새 키라 같은 초안을 덮어쓴다."""
    digest = hashlib.sha256(
        json.dumps(rubric, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"rubric-gen:{event.generation_id}:{rubric['work_type']}:{digest}"


async def generate_drafts(gateway, builder, event: RubricGenerationEvent) -> list[SavedDraft]:
    """요청한 유형마다 기준을 만들어 초안으로 저장한다.

    하나라도 실패하면 예외를 올린다(재시도 때 처음부터 다시 만든다).
    """
    expected = DocumentRef(doc_id=event.policy_doc_id, version=event.policy_version)
    policy = f"{event.policy_doc_id}@{event.policy_version}"
    note = f"내규 {policy} 업로드로 자동 생성({event.generation_id})"
    drafts = []
    for work_type in event.work_types:
        rubric = await generate_mcp_criteria(gateway, builder, work_type, expected_policy=expected)
        reply = await gateway.call("business", "save_rubric", {
            "work_type": rubric["work_type"],
            "items": rubric["items"],
            "idempotency_key": idempotency_key(event, rubric),
            "note": note,
        })
        data = reply["data"]
        drafts.append(SavedDraft(
            work_type=work_type, doc_id=data["doc_id"], version=data["version"],
            replaced_draft=bool(data.get("replaced_draft")),
        ))
    return drafts


class RubricWorker:
    def __init__(
        self,
        execute,
        *,
        company_id: str,
        trace_dir,
        model: str,
        code_provenance: dict,
        langsmith=False,
        max_retries=3,
    ):
        if not 0 <= max_retries <= 10:
            raise ValueError("max retries must be 0..10")
        self.execute, self.company_id, self.trace_dir = execute, company_id, trace_dir
        self.model, self.code_provenance = model, code_provenance
        self.langsmith, self.max_retries = langsmith, max_retries

    async def handle(self, body: bytes, *, publish, ack):
        try:
            if len(body) > 16384:
                raise ValueError("oversized event")
            event = RubricGenerationEvent.model_validate_json(body)
        except (ValidationError, ValueError):
            # 원본은 DLQ에 복사하지 않는다. 생성 건 ID만 읽히면
            # 백엔드가 실패로 기록할 수 있게 남긴다.
            diagnostic = {"message_id": str(uuid4()), "type": "INVALID_RUBRIC_GENERATE"}
            if generation_id := _generation_id(body):
                diagnostic["generation_id"] = generation_id
            await publish(DLQ, diagnostic, "INVALID_EVENT")
            await ack()
            return {"status": "dlq", "code": "INVALID_EVENT"}

        with TraceSession(
            self.trace_dir,
            langsmith=self.langsmith,
            metadata={
                "request_id": event.generation_id,
                "purpose": "rubric",
                "model": self.model,
                "rubric_version": event.policy_version,
                "git_commit": self.code_provenance.get("git_commit", "unknown"),
            },
        ) as trace:
            with trace.span("rubric.request", inputs=event) as span:
                failure = None
                try:
                    if event.company_id != self.company_id:
                        # 토큰 회사와 다른 요청을 처리하면 다른 회사 기준을 덮어쓴다.
                        raise RubricGenerationError("TOKEN_SCOPE_MISMATCH")
                    if event.attempt > self.max_retries:
                        raise RubricGenerationError("RETRY_EXHAUSTED")
                    drafts = await self.execute(event)
                except Exception as exc:
                    failure = classify(exc)
                if failure:
                    retry = failure.retryable and event.attempt < self.max_retries
                    payload = event.model_dump(mode="json")
                    if retry:
                        payload["attempt"] += 1
                    # 브로커의 발행 확인까지 기다린다. 실패 시 원본 메시지를 ack하지 않는다.
                    await publish(RETRY_QUEUE if retry else DLQ, payload, failure.code)
                    result = {"status": "retry" if retry else "dlq", "code": failure.code,
                              "generation_id": event.generation_id, "trace_id": trace.trace_id}
                else:
                    done = RubricGenerationResult(
                        message_id=str(uuid4()), generation_id=event.generation_id,
                        company_id=event.company_id, policy_doc_id=event.policy_doc_id,
                        policy_version=event.policy_version, drafts=drafts,
                    )
                    await publish(RESULTS_QUEUE, done.model_dump(mode="json"), None)
                    result = {"status": "완료", "generation_id": event.generation_id,
                              "drafts": len(drafts), "trace_id": trace.trace_id}
                span.result(result)
                span.outputs["status"] = result["status"]
                if failure:
                    span.outputs["error_code"] = failure.code
            # 저장·완료 발행 후, 실패 시 재시도/DLQ 발행 확인 후에만 ack한다.
            await ack()
            return result


def _generation_id(body: bytes) -> str | None:
    try:
        value = json.loads(body).get("generation_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _ID.fullmatch(value) else None
