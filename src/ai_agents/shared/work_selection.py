"""백엔드에 저장된 사용자 선택. LLM 분류 결과/근거와 구분한다."""

from typing import Literal, Self

from pydantic import model_validator

from .execution_contracts import CanonicalWorkType, canonical_work_type
from .models import NonEmpty, StrictModel, WorkType

INTERNAL_WORK_TYPES = {
    "rfp_analysis": "rfp_analysis",
    "proposal": "proposal",
    "quote": "quotation",
    "inquiry_reply": "customer_response",
}


class UserSelectedWorkType(StrictModel):
    status: Literal["selected"] = "selected"
    source: Literal["user"] = "user"
    registered_work_type: CanonicalWorkType
    work_type: WorkType

    @model_validator(mode="after")
    def consistent_type(self) -> Self:
        if INTERNAL_WORK_TYPES[self.registered_work_type] != self.work_type:
            raise ValueError("저장된 업무 유형과 Agent 내부 유형이 다릅니다.")
        return self


class UserSelectionReport(StrictModel):
    work_id: NonEmpty
    input_sha256: NonEmpty
    # 기존 평가 인계 필드명을 유지하되 status/source로 사용자 선택임을 명시한다.
    classification: UserSelectedWorkType


def selected_work_type(value: str) -> UserSelectedWorkType:
    if not isinstance(value, str):
        raise ValueError("사용자가 선택한 업무 유형이 필요합니다.")
    canonical = canonical_work_type(value)
    return UserSelectedWorkType(
        registered_work_type=canonical,
        work_type=INTERNAL_WORK_TYPES[canonical],
    )
