"""팀 업무 집계 계약 v1. 개인 평가 원문/ID는 받지 않으며 권한 검증은 서버가 담당한다."""

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .execution_contracts import CanonicalWorkType, Digest
from .models import StrictModel

Criterion = Literal[
    "problem_definition", "instruction_quality", "verification", "security", "output_quality",
]
CRITERIA = {
    "problem_definition": "문제 정의", "instruction_quality": "지시 품질",
    "verification": "결과 검증", "security": "보안 준수", "output_quality": "결과물 품질",
}
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
MetricId = Annotated[str, Field(pattern=(
    r"^g[1-9][0-9]*\.(problem_definition|instruction_quality|verification|security|output_quality)$"
))]


class TeamStatistics(StrictModel):
    scored_members: int = Field(ge=2, strict=True)
    scored_evaluations: int = Field(ge=2, strict=True)
    score_sum: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def valid_totals(self) -> Self:
        if (self.scored_members > self.scored_evaluations
                or self.score_sum > 2 * self.scored_evaluations):
            raise ValueError("invalid aggregate totals")
        return self


class TeamCell(StrictModel):
    criterion: Criterion
    # 비공개·미관측은 null. 이유/소수 인원 수/개별 점수는 전달하지 않는다.
    statistics: TeamStatistics | None


class TeamGroup(StrictModel):
    work_type: CanonicalWorkType
    rubric_version: Version
    rubric_sha256: Digest
    members: int = Field(ge=2, strict=True)
    finalized_evaluations: int = Field(ge=2, strict=True)
    cells: list[TeamCell] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.members > self.finalized_evaluations:
            raise ValueError("invalid group population")
        if {cell.criterion for cell in self.cells} != set(CRITERIA):
            raise ValueError("exactly five unique criteria required")
        for cell in self.cells:
            stats = cell.statistics
            if stats and (stats.scored_members > self.members
                          or stats.scored_evaluations > self.finalized_evaluations):
                raise ValueError("cell exceeds group population")
            if stats and cell.criterion == "security" and stats.score_sum % 2:
                raise ValueError("security scores are 0 or 2")
        return self


class TeamSnapshot(StrictModel):
    schema_version: Literal["1"] = "1"
    purpose: Literal["team"] = "team"
    request_id: UUID
    team_id: UUID
    snapshot_id: UUID
    period_start: date
    period_end: date  # exclusive
    source_status: Literal["finalized"] = "finalized"
    deduplication: Literal["latest_final_per_work"] = "latest_final_per_work"
    privacy_policy_version: Version
    min_members: int = Field(default=5, ge=2, le=100, strict=True)
    release_approved: bool = Field(strict=True)
    groups: list[TeamGroup] = Field(max_length=100)

    @model_validator(mode="after")
    def releasable(self) -> Self:
        if self.period_end <= self.period_start:
            raise ValueError("period must be nonempty")
        if not self.release_approved and self.groups:
            raise ValueError("withheld snapshot cannot contain aggregate data")
        keys = [(g.work_type, g.rubric_version, g.rubric_sha256) for g in self.groups]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate rubric group")
        for group in self.groups:
            if group.members < self.min_members:
                raise ValueError("group below privacy threshold")
            for cell in group.cells:
                if cell.statistics and cell.statistics.scored_members < self.min_members:
                    raise ValueError("cell below privacy threshold")
        return self


ActionId = Literal[
    "define_acceptance", "clarify_constraints", "structure_prompt", "request_evidence",
    "cross_check", "review_assumptions", "mask_before_input", "check_sharing_scope",
    "use_review_checklist", "validate_deliverable",
]


class TeamImprovement(StrictModel):
    metric_id: MetricId
    action_id: ActionId


class TeamDecision(StrictModel):
    # 모델은 공개 집계 근거와 코칭 항목만 선택한다. 자유 텍스트/개인 추론은 반환하지 않는다.
    strengths: list[MetricId] = Field(max_length=3)
    improvements: list[TeamImprovement] = Field(max_length=3)


class TeamMetric(StrictModel):
    metric_id: MetricId
    work_type: CanonicalWorkType
    rubric_version: Version
    rubric_sha256: Digest
    criterion: Criterion
    score_sum: int
    scored_evaluations: int
    finalized_evaluations: int
    mean_score: float
    coverage_percent: float
    band: Literal["strength", "improvement"]


class TeamInsight(StrictModel):
    metric_id: MetricId
    observation: str
    action_id: ActionId | None = None
    recommendation: str | None = None


class TeamReport(StrictModel):
    schema_version: Literal["1"] = "1"
    purpose: Literal["team"] = "team"
    request_id: UUID
    team_id: UUID
    snapshot_id: UUID
    snapshot_sha256: Digest
    period_start: date
    period_end: date
    status: Literal["generated", "insufficient_data", "withheld"]
    presentation_version: Literal["team-coaching-v1"] = "team-coaching-v1"
    weighting: Literal["per_evaluation_within_rubric"] = "per_evaluation_within_rubric"
    summary: str
    metrics: list[TeamMetric]
    strengths: list[TeamInsight]
    improvements: list[TeamInsight]
    limitations: list[str]


class TeamSaveRequest(StrictModel):
    request_id: UUID
    snapshot_id: UUID
    idempotency_key: Digest
    report_sha256: Digest
    report: TeamReport


class TeamSaveReceipt(StrictModel):
    request_id: UUID
    snapshot_id: UUID
    report_sha256: Digest
    revision: int = Field(ge=1, strict=True)
    status: Literal["saved"]
