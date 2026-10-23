"""내규 업로드 뒤 평가 기준 초안 생성 큐 계약.

백엔드 rubric_generations·RUBRIC_GENERATE와 같은 칸이다.

요청(RUBRIC_GENERATE)은 백엔드가 rubric_generation.requests로, 완료(RUBRIC_GENERATED)는 Worker가
rubric_generation.results로 보낸다. 식별자·버전만 담고 내규 원문·기준 내용은 넣지 않는다.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from .execution_contracts import CanonicalWorkType
from .live_feedback_contracts import ID
from .models import StrictModel


class RubricGenerationEvent(StrictModel):
    message_id: ID
    type: Literal["RUBRIC_GENERATE"] = "RUBRIC_GENERATE"
    generation_id: ID
    company_id: ID
    # 생성 입력으로 고정한 내규. 검색 결과가 이 버전이 아니면 저장하지 않는다.
    policy_doc_id: ID
    policy_version: ID
    work_types: list[CanonicalWorkType] = Field(min_length=1, max_length=4)
    requested_by: ID
    attempt: int = Field(default=0, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def unique_work_types(self) -> Self:
        if len(set(self.work_types)) != len(self.work_types):
            raise ValueError("duplicate work type")
        return self


class SavedDraft(StrictModel):
    work_type: CanonicalWorkType
    doc_id: ID
    version: ID
    replaced_draft: bool


class RubricGenerationResult(StrictModel):
    message_id: ID
    type: Literal["RUBRIC_GENERATED"] = "RUBRIC_GENERATED"
    generation_id: ID
    company_id: ID
    policy_doc_id: ID
    policy_version: ID
    drafts: list[SavedDraft] = Field(min_length=1, max_length=4)
