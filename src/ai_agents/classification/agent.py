import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ..shared.models import ClassificationReport, ClassificationResult, WorkContext
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT

MAX_INPUT_CHARS = 80_000


class ClassificationError(Exception):
    """입력·모델·근거 문제. 성공 결과로 대체하지 않습니다."""


@dataclass(frozen=True)
class ModelReply:
    result: ClassificationResult
    response_id: str | None = None
    decision_trace: dict | None = None


class ClassificationBackend(Protocol):
    model: str

    async def classify(self, *, system_prompt: str, payload: str) -> ModelReply: ...


def validate_evidence(result: ClassificationResult, context: WorkContext) -> None:
    sources = context.evidence_sources()
    seen: set[tuple[str, str]] = set()
    for evidence in result.evidence:
        if evidence.source_id not in sources:
            raise ClassificationError("분류 결과가 입력에 없는 근거 ID를 인용했습니다.")
        if evidence.quote not in sources[evidence.source_id]:
            raise ClassificationError("인용문이 지정한 근거의 원문과 일치하지 않습니다.")
        key = (evidence.source_id, evidence.quote)
        if key in seen:
            raise ClassificationError("동일한 근거 인용이 중복되었습니다.")
        seen.add(key)


async def classify_work(
    context: WorkContext, backend: ClassificationBackend
) -> ClassificationReport:
    """입력 출처(JSON/MCP)와 모델 SDK에 독립적인 업무 분류 함수."""
    # model_construct 등으로 검증이 우회된 객체도 경계에서 다시 검증합니다.
    context = WorkContext.model_validate(context.model_dump())
    payload = json.dumps(context.llm_payload(), ensure_ascii=False, sort_keys=True)
    if len(payload) > MAX_INPUT_CHARS:
        raise ClassificationError(
            f"입력이 {MAX_INPUT_CHARS:,}자를 초과했습니다. 자동으로 잘라 분류하지 않습니다."
        )

    reply = await backend.classify(system_prompt=SYSTEM_PROMPT, payload=payload)
    result = ClassificationResult.model_validate(reply.result.model_dump())
    validate_evidence(result, context)
    canonical = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return ClassificationReport(
        work_id=context.work_id,
        input_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        model=backend.model,
        prompt_version=PROMPT_VERSION,
        response_id=reply.response_id,
        decision_trace=reply.decision_trace,
        classification=result,
    )

