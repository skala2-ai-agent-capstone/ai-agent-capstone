"""백엔드가 발행하는 최종 평가 작업과 저장 완료 알림. 토큰·원문은 큐에 싣지 않는다."""

from typing import Literal

from pydantic import Field

from .live_feedback_contracts import ID
from .models import StrictModel


class FinalEvaluationEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    type: Literal["FINAL_EVALUATE"] = "FINAL_EVALUATE"
    message_id: ID
    evaluation_id: ID
    operation_id: ID
    attempt: int = Field(default=0, ge=0, le=100, strict=True)


class FinalEvaluationSaved(StrictModel):
    schema_version: Literal["1"] = "1"
    type: Literal["FINAL_EVALUATION_SAVED"] = "FINAL_EVALUATION_SAVED"
    message_id: ID
    evaluation_id: ID
    operation_id: ID
    status: Literal["draft_and_coaching_saved"] = "draft_and_coaching_saved"
    review_required: bool = Field(strict=True)
    draft_revision: int = Field(ge=1, strict=True)
    coaching_revision: int = Field(ge=1, strict=True)
