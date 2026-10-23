"""저장된 루브릭을 기존 평가기의 입력으로 변환한다. 기준 생성·수정·사람 승인을 수행하지 않는다."""

import json
from typing import Literal, Self

from pydantic import Field, model_validator

from ...shared.models import NonEmpty, StrictModel, WorkContext
from ...shared.work_selection import UserSelectedWorkType, selected_work_type
from ...shared.workflow_models import CriteriaDraft, EvaluationInput, content_hash

ITEMS = {
    "문제 정의": ("problem_definition", "employee_behavior"),
    "지시 품질": ("instruction_quality", "employee_behavior"),
    "결과 검증": ("verification", "employee_behavior"),
    "보안 준수": ("security", "information_handling"),
    "결과물 품질": ("quality", "artifact_quality"),
}


class StoredRubricItem(StrictModel):
    item_id: NonEmpty
    item: NonEmpty
    criterion: NonEmpty
    score_2: NonEmpty
    score_1: str | None = None
    score_0: NonEmpty
    unobserved: NonEmpty
    needs_review: str | None = None
    evidence_sources: list[NonEmpty]
    related_clauses: list[NonEmpty]


class StoredRubric(StrictModel):
    doc_id: NonEmpty
    version: NonEmpty
    work_type: Literal["rfp_analysis", "proposal", "quote", "inquiry_reply"]
    status: Literal["현행", "폐기"]  # 고정된 이전 버전은 허용하되 초안은 사용하지 않는다.
    content_type: Literal["data"]
    items: list[StoredRubricItem] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({i.item_id for i in self.items}) != 5 or {i.item for i in self.items} != set(ITEMS):
            raise ValueError("저장된 기준은 중복 없는 5개 평가 항목이어야 합니다.")
        for i in self.items:
            if i.item == "보안 준수" and i.score_1 not in (None, "", "해당 없음"):
                raise ValueError("보안 기준의 1점 조건은 허용하지 않습니다.")
            if i.item != "보안 준수" and not (i.score_1 and i.score_1.strip()):
                raise ValueError("일반 항목에는 0·1·2점 조건이 모두 필요합니다.")
        return self

    def criteria(self) -> CriteriaDraft:
        selection = selected_work_type(self.work_type)
        references, items = [], []
        for row in self.items:
            key, dimension = ITEMS[row.item]
            # 기준·점수 조건·미관측 조건·내규 연결을 모두 보존한다. 내용은 재작성하지 않는다.
            text = json.dumps(row.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            ref = {"document_id": self.doc_id, "version": self.version, "section_id": row.item_id}
            references.append({**ref, "content": text})
            items.append(
                {
                    "id": f"{selection.work_type}.{key}",
                    "title": row.item,
                    "dimension": dimension,
                    "levels": [
                        {"score": n, "condition": getattr(row, f"score_{n}")}
                        for n in ((0, 2) if key == "security" else (0, 1, 2))
                    ],
                    "unobserved_condition": row.unobserved,
                    "review_condition": row.needs_review or "",
                    "evidence_sources": row.evidence_sources,
                    "rule_evidence": [{**ref, "quote": text}],
                }
            )
        return CriteriaDraft(
            work_type=selection.work_type,
            references=references,
            items=items,
            generation={
                "source": "stored_rubric",
                "doc_id": self.doc_id,
                "version": self.version,
                "rubric_sha256": content_hash(self),
            },
        )

    def item_ids(self) -> dict[str, str]:
        work_type = selected_work_type(self.work_type).work_type
        return {f"{work_type}.{ITEMS[row.item][0]}": row.item_id for row in self.items}


class StoredEvaluationInput(StrictModel):
    """사람 승인 입력과 구분되는 저장 기준 입력. approval을 만들어 내지 않는다."""

    run_id: NonEmpty
    work_record: WorkContext
    classification: UserSelectedWorkType
    stored_rubric: StoredRubric
    approved_criteria: CriteriaDraft  # 기존 평가기가 사용하는 필드명; 원본은 stored_rubric.

    @model_validator(mode="after")
    def bound(self) -> Self:
        if self.classification.registered_work_type != self.stored_rubric.work_type:
            raise ValueError("업무 유형과 저장된 기준의 유형이 다릅니다.")
        if self.approved_criteria != self.stored_rubric.criteria():
            raise ValueError("저장된 기준을 임의로 변경할 수 없습니다.")
        return self


def checked_input(request):
    model = StoredEvaluationInput if isinstance(request, StoredEvaluationInput) else EvaluationInput
    return model.model_validate(request.model_dump(mode="json"))
