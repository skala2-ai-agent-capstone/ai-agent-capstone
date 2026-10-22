"""버튼 요청·폴링형 중간 피드백 계약. 최종 평가/기준 생성 계약과 독립적이다."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .execution_contracts import CanonicalWorkType
from .models import StrictModel

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
Positive = Annotated[int, Field(ge=1, strict=True)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class LiveEvaluationEvent(StrictModel):
    message_id: ID
    type: Literal["LIVE_EVALUATE"] = "LIVE_EVALUATE"
    live_eval_id: ID
    company_id: ID
    work_id: ID
    requested_by: ID
    from_seq: Positive
    to_seq: Positive
    rubric_version: ID | None = None
    attempt: int = Field(default=0, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.from_seq > self.to_seq:
            raise ValueError("invalid conversation range")
        return self


class RubricItem(StrictModel):
    item_id: ID
    item: Text
    criterion: Text


class FeedbackRubric(StrictModel):
    doc_id: ID
    version: ID
    items: list[RubricItem] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        if len({i.item_id for i in self.items}) != len(self.items):
            raise ValueError("duplicate rubric item")
        return self


class LiveEvaluationContext(StrictModel):
    live_eval_id: ID
    company_id: ID
    work_id: ID
    requested_by: ID
    requested_at: AwareDatetime
    from_seq: Positive
    to_seq: Positive
    status: Literal["대기", "생성 중", "완료", "실패"]
    work_type: CanonicalWorkType | None = None
    rubric_doc_id: ID | None = None
    rubric_version: ID | None = None
    rubric: FeedbackRubric | None = None

    @model_validator(mode="after")
    def bound_rubric(self) -> Self:
        if self.from_seq > self.to_seq:
            raise ValueError("invalid range")
        if (self.rubric_doc_id is None) != (self.rubric_version is None):
            raise ValueError("rubric reference must contain document and version")
        if self.rubric is not None and (
            self.rubric.doc_id != self.rubric_doc_id or self.rubric.version != self.rubric_version
        ):
            raise ValueError("rubric reference mismatch")
        if self.status == "생성 중" and self.rubric_doc_id and self.rubric is None:
            raise ValueError("pinned rubric cannot be silently omitted")
        return self


class ChatMessage(StrictModel):
    message_id: ID
    exchange_id: Positive
    seq: Positive
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=12000)]
    created_at: AwareDatetime


class MessageBatch(StrictModel):
    live_eval_id: ID
    work_id: ID
    from_seq: Positive
    to_seq: Positive
    masking_applied: bool = Field(strict=True)
    masking_version: ID
    complete: bool = Field(strict=True)
    messages: list[ChatMessage] = Field(max_length=10)


class FeedbackEvidence(StrictModel):
    message_id: ID
    quote: Annotated[str, Field(min_length=1, max_length=500)]


class LiveFeedbackItem(StrictModel):
    item_id: ID
    criterion_id: ID | None = None
    kind: Literal["strength", "improvement", "needs_check"]
    diagnosis: Text
    evidence: list[FeedbackEvidence] = Field(min_length=1, max_length=4)


class PromptCoaching(StrictModel):
    item_id: ID
    target_message_id: ID
    next_action: Text
    suggested_prompt: Text


class GeneratedFeedback(StrictModel):
    items: list[LiveFeedbackItem] = Field(min_length=1, max_length=3)
    coaching: list[PromptCoaching] = Field(min_length=1, max_length=3)


class SaveReceipt(StrictModel):
    live_eval_id: ID
    status: Literal["완료", "실패"]
    duplicate: bool = Field(strict=True)
