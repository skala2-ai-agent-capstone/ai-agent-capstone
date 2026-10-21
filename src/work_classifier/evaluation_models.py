from typing import Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from .models import NonEmpty, StrictModel
from .workflow_models import EvaluationOutput, ItemEvaluation, Reference


class AgentItemEvaluation(ItemEvaluation):
    """기존 항목별 결과에 점수 제한을 추가한다."""

    score: int | None = Field(ge=0, le=2, strict=True)

    @model_validator(mode="after")
    def check_security_score(self) -> Self:
        if self.criterion_id.endswith(".security"):
            if self.score == 1:
                raise ValueError("보안 준수 항목에는 1점을 줄 수 없습니다.")

        return self


class AgentEvaluationOutput(EvaluationOutput):
    """기존 전체 결과에 항목 개수와 중복 검사를 추가한다."""

    items: list[AgentItemEvaluation] = Field(
        min_length=5,
        max_length=5,
    )

    @model_validator(mode="after")
    def check_unique_items(self) -> Self:
        item_ids = [item.criterion_id for item in self.items]

        if len(item_ids) != len(set(item_ids)):
            raise ValueError("평가 항목 ID가 중복되었습니다.")

        return self


class EvaluationReceipt(StrictModel):
    """평가 코드가 검증 후 생성하는 결과·근거 묶음. LLM 출력 스키마와 분리한다."""

    model_config = ConfigDict(str_strip_whitespace=False)

    report: EvaluationOutput
    approved_business_sha256: NonEmpty
    snapshot: dict[str, JsonValue]
    materials_sha256: NonEmpty
    additional_sources: dict[str, str]
    additional_references: list[Reference]
