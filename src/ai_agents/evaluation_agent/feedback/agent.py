"""OpenAI 호환 Chat Completions를 제공하는 자체 호스팅 모델 어댑터."""

import json

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from ...observability import trace_step
from ...observability.tracing import trace_span
from ...shared.live_feedback_contracts import GeneratedFeedback
from .errors import FeedbackError
from .normalize import normalize_feedback
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .validation import SENSITIVE, validate_feedback, validate_messages


class FeedbackAgent:
    def __init__(self, client, model: str, *, record_work_refs: bool = True):
        self.client, self.model = client, model
        self.prompt_version = PROMPT_VERSION
        # 가상 평가셋은 DB에 존재하지 않는 업무·사용자 ID를 프록시에 보내지 않는다.
        self.record_work_refs = record_work_refs

    @trace_step("feedback.generate")
    async def generate(self, context, batch):
        turns = validate_messages(context, batch)
        rubric = context.rubric.model_dump(mode="json") if context.rubric else None
        if rubric and SENSITIVE.search(json.dumps(rubric, ensure_ascii=False)):
            raise FeedbackError("UNMASKED_INPUT")
        headers = {"X-Stage": "live_evaluate"}
        if self.record_work_refs:
            # 프록시의 header_tag가 128자로 자르므로 잘린 ID로 기록되지 않게 한다.
            if any(len(value) > 128 for value in (context.work_id, context.requested_by)):
                raise FeedbackError("CONTRACT_MISMATCH")
            headers.update({"X-Work-Id": context.work_id, "X-User-Id": context.requested_by})
        payload = {"turns": turns, "rubric": rubric, "work_type": context.work_type}
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
                + json.dumps(GeneratedFeedback.model_json_schema(), ensure_ascii=False),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(
            2
        ):  # 구조·근거 오류만 한 번 재생성. 기준 생성 Agent는 호출하지 않는다.
            try:
                with trace_span("feedback.llm", inputs=messages, run_type="llm") as span:
                    reply = await self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=0,
                        max_tokens=1800,
                        response_format={"type": "json_object"},
                        extra_headers=headers,
                    )
                    span.result(reply)
                    usage = getattr(reply, "usage", None)
                    span.outputs["usage_metadata"] = {
                        new: value
                        for old, new in (
                            ("prompt_tokens", "input_tokens"),
                            ("completion_tokens", "output_tokens"),
                            ("total_tokens", "total_tokens"),
                        )
                        if type(value := getattr(usage, old, None)) is int and value >= 0
                    }
                if not reply.choices or reply.choices[0].finish_reason != "stop":
                    raise FeedbackError("LLM_REJECTED")
                raw = reply.choices[0].message.content
                if not isinstance(raw, str) or len(raw) > 30000:
                    raise FeedbackError("INVALID_FEEDBACK")
                # 근거 연결(인용 위치·코칭 대상)만 바로잡고, 검증은 그대로 엄격하게 한다
                result, repairs = normalize_feedback(
                    GeneratedFeedback.model_validate_json(raw), batch
                )
                with trace_span("feedback.validate") as span:
                    validate_feedback(result, context, batch)
                    span.result({"valid": True})
                    span.outputs["repairs"] = repairs
                return result
            except (APIConnectionError, APITimeoutError):
                raise FeedbackError("LLM_UNAVAILABLE", retryable=True) from None
            except APIStatusError as exc:
                retryable = exc.status_code in {408, 429} or exc.status_code >= 500
                raise FeedbackError(
                    "LLM_UNAVAILABLE" if retryable else "LLM_REJECTED", retryable=retryable
                ) from None
            except (ValidationError, FeedbackError) as exc:
                code = exc.code if isinstance(exc, FeedbackError) else "INVALID_FEEDBACK"
                if attempt or code == "LLM_REJECTED":
                    raise FeedbackError(code) from None
                # 잘못된 모델 원문/예외를 프롬프트·로그에 되넣지 않는다.
                # 허용되는 ID만 다시 알려 준다.
                user_ids = ", ".join(m.message_id for m in batch.messages if m.role == "user")
                messages.append(
                    {
                        "role": "user",
                        "content": "이전 결과의 형식 또는 인용 검증에 실패했습니다. "
                        "다시 작성하세요.\n"
                        f"- coaching.target_message_id는 사용자 메시지 ID 중 하나만: {user_ids}\n"
                        "- coaching.item_id는 items의 item_id를 그대로 쓰고, "
                        "improvement마다 코칭을 하나씩 둡니다.\n"
                        "- quote는 message_id가 가리키는 메시지에서 "
                        "글자 그대로 복사한 짧은 구간입니다.",
                    }
                )
        raise AssertionError("unreachable")
