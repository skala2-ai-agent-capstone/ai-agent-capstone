"""고정된 최근 5회에 대한 중간 피드백. 최종 평가·기준 생성 흐름은 호출하지 않는다."""

import asyncio
import hashlib
import json
from time import perf_counter

from pydantic import ValidationError

from ..evaluation_agent.feedback.errors import FeedbackError
from ..evaluation_agent.feedback.prompts import PROMPT_VERSION
from ..evaluation_agent.feedback.validation import validate_context, validate_messages
from ..observability import trace_step
from ..shared.live_feedback_contracts import (
    LiveEvaluationContext,
    MessageBatch,
    SaveReceipt,
)


def content_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def retry_transient(operation, *, sleep=asyncio.sleep):
    for attempt in range(2):  # 최초 호출 + 1회. 영구 오류는 재호출하지 않는다.
        try:
            return await operation()
        except FeedbackError as exc:
            if not exc.retryable or attempt:
                raise
            await sleep(0.5)
    raise AssertionError("unreachable")


class LiveFeedbackWorkflow:
    def __init__(self, gateway, agent, *, sleep=asyncio.sleep):
        self.gateway, self.agent, self.sleep = gateway, agent, sleep

    async def call(self, tool, arguments):
        return await retry_transient(lambda: self.gateway.call(tool, arguments), sleep=self.sleep)

    @trace_step("feedback.workflow")
    async def run(self, event, *, trace_id: str, code_provenance: dict):
        started = perf_counter()
        try:
            context = LiveEvaluationContext.model_validate(
                await self.call(
                    "start_live_evaluation",
                    {"live_eval_id": event.live_eval_id},
                )
            )
            validate_context(event, context)
            if context.status in {"완료", "실패"}:
                return {
                    "live_eval_id": event.live_eval_id,
                    "status": context.status,
                    "duplicate": True,
                    "trace_id": trace_id,
                }
            batch = MessageBatch.model_validate(
                await self.call(
                    "get_messages",
                    {
                        "live_eval_id": event.live_eval_id,
                        "work_id": event.work_id,
                        "from_seq": event.from_seq,
                        "to_seq": event.to_seq,
                    },
                )
            )
            turns = validate_messages(context, batch)
            result = await retry_transient(
                lambda: self.agent.generate(context, batch), sleep=self.sleep
            )
            rubric = context.rubric.model_dump(mode="json") if context.rubric else None
            provenance = {
                "trace_id": trace_id,
                "prompt_version": PROMPT_VERSION,
                "model": self.agent.model,
                "model_settings": {"temperature": 0, "max_tokens": 1800},
                "rubric_doc_id": context.rubric_doc_id,
                "rubric_version": context.rubric_version,
                "rubric_sha256": content_digest(rubric),
                "mode": "rubric_coaching" if rubric else "general_prompt_coaching",
                "from_seq": context.from_seq,
                "to_seq": context.to_seq,
                "exchange_ids": [t["exchange_id"] for t in turns],
                "message_ids": [m.message_id for m in sorted(batch.messages, key=lambda m: m.seq)],
                "source_sha256": content_digest(batch.model_dump(mode="json")),
                "masking_version": batch.masking_version,
                "code": code_provenance,
            }
            # coaching JSONB의 객체 구조는 프론트/MCP와 공유하는 v1 계약이다.
            payload = {
                "live_eval_id": event.live_eval_id,
                "status": "완료",
                "items": [i.model_dump(mode="json") for i in result.items],
                "coaching": {
                    "schema_version": "1",
                    "provenance": provenance,
                    "suggestions": [c.model_dump(mode="json") for c in result.coaching],
                },
                "model": self.agent.model,
                "latency_ms": round((perf_counter() - started) * 1000),
                "error": None,
            }
            receipt = SaveReceipt.model_validate(await self.call("save_live_evaluation", payload))
            if receipt.live_eval_id != event.live_eval_id:
                raise FeedbackError("SCOPE_MISMATCH")
            return {**receipt.model_dump(mode="json"), "trace_id": trace_id}
        except ValidationError:
            raise FeedbackError("CONTRACT_MISMATCH") from None
