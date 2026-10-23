from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from ..shared.llm_trace import trace_headers
from .agent import ClassificationError, ModelReply
from .selection import (
    SELECTION_VERSION,
    DecisionEnvelope,
    model_payload,
    resolve_decision,
    response_model,
)

MAX_MODEL_PAYLOAD_CHARS = 240_000


class OpenAIClassifier:
    def __init__(self, client: AsyncOpenAI, model: str):
        self.client = client
        self.model = model

    async def classify(self, *, system_prompt: str, payload: str) -> ModelReply:
        enriched_payload, catalog = model_payload(payload)
        if len(enriched_payload) > MAX_MODEL_PAYLOAD_CHARS:
            raise ClassificationError("근거 목록을 포함한 모델 입력이 크기 제한을 초과했습니다.")
        try:
            schema_model = response_model(catalog)
        except ValueError as exc:
            raise ClassificationError(str(exc)) from exc
        try:
            response = await self.client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": enriched_payload},
                ],
                text_format=schema_model,
                max_output_tokens=2000,
                store=False,
                extra_headers=trace_headers("classify"),
            )
        except APITimeoutError as exc:
            raise ClassificationError("LLM 요청 시간이 초과되었습니다.") from exc
        except APIConnectionError as exc:
            raise ClassificationError("LLM API에 연결할 수 없습니다.") from exc
        except APIStatusError as exc:
            # API 오류 본문에 입력·키 등이 포함될 수 있으므로 그대로 출력하지 않습니다.
            raise ClassificationError(
                f"LLM API 오류(HTTP {exc.status_code}). 키·모델 접근 권한·한도를 확인하세요."
            ) from exc
        except ValidationError as exc:
            raise ClassificationError("LLM 출력이 분류 결과 Schema를 충족하지 않습니다.") from exc

        if response.status != "completed":
            raise ClassificationError(
                "LLM 응답이 완료되지 않았습니다. 출력 한도 또는 응답 상태를 확인하세요."
            )
        for item in response.output:
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "refusal":
                    raise ClassificationError("모델이 분류 응답을 거절했습니다.")
        if response.output_parsed is None:
            raise ClassificationError("LLM 응답에 구조화된 분류 결과가 없습니다.")
        try:
            parsed = DecisionEnvelope.model_validate(response.output_parsed.model_dump(mode="json"))
            result = resolve_decision(parsed, catalog)
        except (ValidationError, KeyError, ValueError) as exc:
            raise ClassificationError("모델 판정 또는 선택한 근거 ID가 유효하지 않습니다.") from exc
        return ModelReply(
            result=result, response_id=response.id,
            decision_trace={"selection_version": SELECTION_VERSION,
                            "normalizations": ["same_type_tasks_merged"]
                            if parsed.decision.status == "multiple_tasks"
                            and result.status == "classified" else [],
                            **parsed.model_dump(mode="json")},
        )

