from typing import Self

from pydantic import Field, model_validator

from .workflow_models import EvaluationOutput, ItemEvaluation


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