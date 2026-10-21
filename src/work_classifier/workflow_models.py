"""A/B 사이의 제안 계약. 실제 MCP의 확정 명세와는 별개입니다."""

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from .models import ClassificationResult, Evidence, NonEmpty, StrictModel, WorkContext, WorkType


def content_hash(value: StrictModel) -> str:
    raw = json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Reference(StrictModel):
    document_id: NonEmpty
    version: NonEmpty
    section_id: NonEmpty
    content: NonEmpty


class RuleEvidence(StrictModel):
    document_id: NonEmpty
    version: NonEmpty
    section_id: NonEmpty
    quote: NonEmpty

    def key(self) -> tuple[str, str, str]:
        return self.document_id, self.version, self.section_id


class ScoreLevel(StrictModel):
    score: int = Field(ge=0, strict=True)
    condition: NonEmpty


class Criterion(StrictModel):
    id: NonEmpty
    title: NonEmpty
    dimension: Literal["employee_behavior", "artifact_quality", "information_handling"]
    levels: list[ScoreLevel] = Field(min_length=1)
    unobserved_condition: NonEmpty
    review_condition: str = ""
    evidence_sources: list[NonEmpty] = Field(default_factory=list)
    rule_evidence: list[RuleEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_scores(self) -> Self:
        scores = [level.score for level in self.levels]
        if len(scores) != len(set(scores)):
            raise ValueError("동일 평가 항목의 점수 조건은 중복할 수 없습니다.")
        return self


class CriteriaDraft(StrictModel):
    work_type: WorkType
    references: list[Reference] = Field(min_length=1)
    items: list[Criterion] = Field(min_length=1)
    generation: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_references(self) -> Self:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("평가 항목 ID는 고유해야 합니다.")
        sources = {(r.document_id, r.version, r.section_id): r.content for r in self.references}
        if len(sources) != len(self.references):
            raise ValueError("문서·버전·구간 조합은 고유해야 합니다.")
        for item in self.items:
            for evidence in item.rule_evidence:
                if evidence.key() not in sources or evidence.quote not in sources[evidence.key()]:
                    raise ValueError("기준 근거의 문서·버전·구간 또는 인용문이 일치하지 않습니다.")
        return self


class Approval(StrictModel):
    status: Literal["approved"]
    run_id: NonEmpty
    work_sha256: NonEmpty
    criteria_sha256: NonEmpty
    revision: int = Field(ge=1, strict=True)
    approved_at: datetime


class EvaluationInput(StrictModel):
    run_id: NonEmpty
    work_record: WorkContext
    classification: ClassificationResult
    approved_criteria: CriteriaDraft
    approval: Approval

    @model_validator(mode="after")
    def check_approval(self) -> Self:
        if self.classification.status != "classified":
            raise ValueError(
                "평가 전 단일 업무 확정이 필요합니다. 복합 업무는 범위 분리 후 요청하세요."
            )
        if self.classification.work_type != self.approved_criteria.work_type:
            raise ValueError("분류와 평가기준의 업무 유형이 다릅니다.")
        if self.run_id != self.approval.run_id:
            raise ValueError("다른 실행의 승인입니다.")
        if content_hash(self.work_record) != self.approval.work_sha256:
            raise ValueError("승인 이후 업무 기록이 변경되었습니다.")
        if content_hash(self.approved_criteria) != self.approval.criteria_sha256:
            raise ValueError("승인 이후 평가기준이 변경되었습니다.")
        if self.approval.approved_at.utcoffset() is None:
            raise ValueError("승인 시각에는 시간대가 필요합니다.")
        return self


class ItemEvaluation(StrictModel):
    criterion_id: NonEmpty
    observation: Literal["observed", "unobserved", "insufficient_data", "needs_review"]
    score: int | None = Field(ge=0, strict=True)
    reason: NonEmpty
    work_evidence: list[Evidence]
    rule_evidence: list[RuleEvidence] = Field(min_length=1)
    review_required: bool = Field(strict=True)

    @model_validator(mode="after")
    def check_observation(self) -> Self:
        if self.observation == "observed":
            if self.score is None or not self.work_evidence:
                raise ValueError("관측된 항목에는 점수와 업무 근거가 필요합니다.")
        elif self.score is not None:
            raise ValueError("미관측·자료 부족 항목에는 점수를 부여할 수 없습니다.")
        if self.observation == "needs_review" and not self.review_required:
            raise ValueError("검토 필요 항목은 review_required가 true여야 합니다.")
        return self


class Coaching(StrictModel):
    criterion_id: NonEmpty
    action: NonEmpty


class EvaluationOutput(StrictModel):
    run_id: NonEmpty
    criteria_sha256: NonEmpty
    items: list[ItemEvaluation] = Field(min_length=1)
    coaching: list[Coaching]
    summary: NonEmpty
