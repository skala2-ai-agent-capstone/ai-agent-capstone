"""RabbitMQ와 독립적으로 검증할 수 있는 수신·재시도·DLQ·ack 처리."""

from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ..evaluation_agent.feedback.errors import FeedbackError
from ..evaluation_agent.feedback.prompts import PROMPT_VERSION
from ..observability import TraceSession
from ..shared.live_feedback_contracts import LiveEvaluationEvent

REQUEST_QUEUE = "live_evaluation.requests"
RETRY_QUEUE = "live_evaluation.retry"
DLQ = "live_evaluation.dlq"


class FeedbackWorker:
    def __init__(
        self,
        execute,
        *,
        trace_dir: Path,
        model: str,
        code_provenance: dict,
        langsmith=False,
        max_retries=3,
    ):
        if not 0 <= max_retries <= 10:
            raise ValueError("max retries must be 0..10")
        self.execute, self.trace_dir, self.model = execute, trace_dir, model
        self.code_provenance, self.langsmith = code_provenance, langsmith
        self.max_retries = max_retries

    async def handle(self, body: bytes, *, publish, ack):
        try:
            if len(body) > 16384:
                raise ValueError("oversized event")
            event = LiveEvaluationEvent.model_validate_json(body)
        except (ValidationError, ValueError):
            # 잘못된 입력에는 대화 원문/토큰이 있을 수 있어 원본을 DLQ에 복사하지 않는다.
            diagnostic = {"message_id": str(uuid4()), "type": "INVALID_LIVE_EVALUATE"}
            await publish(DLQ, diagnostic, "INVALID_EVENT")
            await ack()
            return {"status": "dlq", "code": "INVALID_EVENT"}

        with TraceSession(
            self.trace_dir,
            langsmith=self.langsmith,
            metadata={
                "request_id": event.live_eval_id,
                "purpose": "feedback",
                "model": self.model,
                "prompt_version": PROMPT_VERSION,
                "rubric_version": event.rubric_version or "none",
                "git_commit": self.code_provenance.get("git_commit", "unknown"),
            },
        ) as trace:
            with trace.span("feedback.request", inputs=event) as span:
                failure = None
                try:
                    if event.attempt > self.max_retries:
                        raise FeedbackError("UNEXPECTED_ERROR")
                    result = await self.execute(event, trace.trace_id, self.code_provenance)
                except FeedbackError as exc:
                    failure = exc
                except Exception:
                    failure = FeedbackError("UNEXPECTED_ERROR")
                if failure:
                    retry = failure.retryable and event.attempt < self.max_retries
                    payload = event.model_dump(mode="json")
                    if retry:
                        payload["attempt"] += 1
                    # 브로커의 발행 확인까지 기다린다. 실패 시 원본 메시지를 ack하지 않는다.
                    await publish(RETRY_QUEUE if retry else DLQ, payload, failure.code)
                    result = {
                        "status": "retry" if retry else "dlq",
                        "code": failure.code,
                        "live_eval_id": event.live_eval_id,
                        "trace_id": trace.trace_id,
                    }
                span.result(result)
                span.outputs["status"] = result["status"]
                if failure:
                    span.outputs["error_code"] = failure.code
            # 성공 시 MCP 저장 후, 실패 시 재시도/DLQ 발행 확인 후에만 ack한다.
            await ack()
            return result
