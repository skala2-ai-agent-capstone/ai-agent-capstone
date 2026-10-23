"""개인 평가 페이지의 내용 계약. 사용자 권한·확정 상태·버튼 권한은 서버 응답의 책임이다."""

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .execution_contracts import CanonicalWorkType
from .models import NonEmpty, StrictModel
from .workflow_models import ScoreLevel, content_hash

ITEM_NAMES = ("문제 정의", "지시 품질", "결과 검증", "보안 준수", "결과물 품질")
OBSERVATION_LABELS = {
    "observed": "채점", "unobserved": "미관측",
    "insufficient_data": "자료 부족", "needs_review": "검토 필요",
}
Observation = Literal["observed", "unobserved", "insufficient_data", "needs_review"]


class PageWork(StrictModel):
    work_id: NonEmpty
    title: NonEmpty
    work_type: CanonicalWorkType
    work_type_label: NonEmpty
    artifact_id: NonEmpty
    artifact_version: NonEmpty


class PageRubric(StrictModel):
    doc_id: NonEmpty
    version: NonEmpty
    criteria_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PageWorkEvidence(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    source_id: NonEmpty
    source_kind: NonEmpty
    quote: NonEmpty


class PageRuleEvidence(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    document_id: NonEmpty
    version: NonEmpty
    section_id: NonEmpty
    quote: NonEmpty


class PageItem(StrictModel):
    item_id: NonEmpty  # rubric_items의 원본 ID. 이의 제기에서도 이 ID를 사용한다.
    title: NonEmpty
    criterion: NonEmpty
    score_levels: list[ScoreLevel] = Field(min_length=2, max_length=3)
    unobserved_condition: NonEmpty
    review_condition: str
    observation: Observation
    status_label: NonEmpty
    score: int | None = Field(ge=0, le=2, strict=True)
    max_score: Literal[2] = 2
    included_in_total: bool = Field(strict=True)
    reason: NonEmpty
    review_required: bool = Field(strict=True)
    work_evidence: list[PageWorkEvidence]
    rule_evidence: list[PageRuleEvidence] = Field(min_length=1)
    coaching: NonEmpty

    @model_validator(mode="after")
    def consistent(self) -> Self:
        scored = self.observation == "observed"
        if (self.title not in ITEM_NAMES
                or self.status_label != OBSERVATION_LABELS[self.observation]):
            raise ValueError("invalid item label")
        if self.included_in_total != scored or (self.score is not None) != scored:
            raise ValueError("unscored items cannot contribute to the total")
        if self.review_required != (self.observation in {"needs_review", "insufficient_data"}):
            raise ValueError("inconsistent review requirement")
        levels = [level.score for level in self.score_levels]
        if levels != ([0, 2] if self.title == "보안 준수" else [0, 1, 2]):
            raise ValueError("invalid score levels")
        if scored and (not self.work_evidence or self.score not in levels):
            raise ValueError("scored item requires evidence and an allowed score")
        return self


class PageScore(StrictModel):
    earned: int | None = Field(ge=0, le=10, strict=True)
    possible: int = Field(ge=0, le=10, strict=True)
    scored_count: int = Field(ge=0, le=5, strict=True)
    excluded_count: int = Field(ge=0, le=5, strict=True)
    total_count: Literal[5] = 5
    coverage: Literal["complete", "partial", "none"]
    display: NonEmpty


def score_for(items: list[PageItem]) -> PageScore:
    scored = [item for item in items if item.included_in_total]
    earned = sum(item.score for item in scored) if scored else None
    possible = len(scored) * 2
    return PageScore(
        earned=earned, possible=possible, scored_count=len(scored), excluded_count=5 - len(scored),
        coverage="complete" if len(scored) == 5 else "partial" if scored else "none",
        display=f"{earned}/{possible}" if scored else "산정 불가",
    )


class FinalEvaluationPage(StrictModel):
    schema_version: Literal["1"] = "1"
    evaluation_id: NonEmpty
    operation_id: NonEmpty
    source_run_id: NonEmpty
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    work: PageWork
    rubric: PageRubric
    score: PageScore
    summary: NonEmpty
    review_required: bool = Field(strict=True)
    items: list[PageItem] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if tuple(item.title for item in self.items) != ITEM_NAMES:
            raise ValueError("five items must follow display order")
        if len({item.item_id for item in self.items}) != 5:
            raise ValueError("duplicate rubric item IDs")
        if self.score != score_for(self.items):
            raise ValueError("incorrect score summary")
        if self.review_required != any(item.review_required for item in self.items):
            raise ValueError("incorrect report review flag")
        return self


class FinalReportSaveRequest(StrictModel):
    """MCP 담당자에게 제안할 저장 인자. 현재 서버에 없는 도구를 호출하지 않는다."""

    evaluation_id: NonEmpty
    operation_id: NonEmpty
    draft_revision: int = Field(ge=1, strict=True)
    coaching_revision: int = Field(ge=1, strict=True)
    report: FinalEvaluationPage
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: NonEmpty

    @model_validator(mode="after")
    def bound(self) -> Self:
        if (self.evaluation_id, self.operation_id) != (
            self.report.evaluation_id, self.report.operation_id,
        ) or self.report_sha256 != content_hash(self.report):
            raise ValueError("report scope or hash mismatch")
        return self
