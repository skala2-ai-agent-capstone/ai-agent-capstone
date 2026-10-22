"""서비스 실행 계약 v1. 기존 업무별 기준·채점·승인 모델을 변경하지 않는다.

이 모델은 서버가 인증한 요청의 구조를 검사한다. 서명/사용자 권한 확인이나
승인 원장의 조회를 대신하지 않는다. 토큰과 원문은 실행 계약에 넣지 않는다.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Purpose = Literal["feedback", "final"]
CanonicalWorkType = Literal["rfp_analysis", "proposal", "quote", "inquiry_reply"]


def canonical_work_type(value: str) -> CanonicalWorkType:
    aliases = {"quotation": "quote", "customer_response": "inquiry_reply"}
    value = aliases.get(value, value)
    if value not in {"rfp_analysis", "proposal", "quote", "inquiry_reply"}:
        raise ValueError("unsupported work type")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DocumentRef(Contract):
    doc_id: Identifier
    version: Identifier


class ArtifactRef(Contract):
    artifact_id: Identifier
    version: Identifier


class ConversationRange(Contract):
    from_seq: int = Field(ge=0, strict=True)
    to_seq: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if (self.from_seq == 0) != (self.to_seq == 0) or self.to_seq < self.from_seq:
            raise ValueError("empty range must be 0..0; otherwise use 1 <= from_seq <= to_seq")
        return self


class ApprovedRubricRef(Contract):
    """승인된 기준의 참조만 보관. 실제 승인 여부는 서버 원장과 대조해야 한다."""

    rubric_id: Identifier
    version: Identifier
    work_type: CanonicalWorkType
    content_sha256: Digest
    approval_id: Identifier
    approved_at: AwareDatetime


class ExecutionSnapshot(Contract):
    snapshot_id: Identifier
    work_id: Identifier
    captured_at: AwareDatetime
    conversation_range: ConversationRange
    artifact_ref: ArtifactRef | None = None
    source_refs: tuple[DocumentRef, ...] = ()
    reference_refs: tuple[DocumentRef, ...] = ()
    policy_refs: tuple[DocumentRef, ...] = ()
    rubric: ApprovedRubricRef
    # 업무 등록·근거 연결 등 버전 없는 데이터까지 서버가 고정한 전체 내용의 해시.
    content_sha256: Digest

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        for refs in (self.source_refs, self.reference_refs, self.policy_refs):
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate document/version reference")
        if self.rubric.approved_at > self.captured_at:
            raise ValueError("rubric must be approved before snapshot capture")
        return self


class EvaluationRequest(Contract):
    schema_version: Literal["1"] = "1"
    request_id: Identifier
    purpose: Purpose
    work_type: CanonicalWorkType
    snapshot: ExecutionSnapshot
    idempotency_key: Identifier

    @model_validator(mode="after")
    def compatible_scope(self) -> Self:
        if self.work_type != self.snapshot.rubric.work_type:
            raise ValueError("request and rubric work types differ")
        if self.purpose == "final" and self.snapshot.artifact_ref is None:
            raise ValueError("final evaluation requires an artifact version")
        return self


class FeedbackItem(Contract):
    item_id: Identifier
    kind: Literal["strength", "improvement", "needs_check", "record_guidance"]
    target_ids: tuple[Identifier, ...] = ()
    evidence_ids: tuple[Identifier, ...] = ()
    diagnosis: Annotated[str, Field(min_length=1)]
    next_action: Annotated[str, Field(min_length=1)]
    suggested_prompt: str | None = None

    @model_validator(mode="after")
    def grounded(self) -> Self:
        if self.kind in {"strength", "improvement"} and (
            not self.target_ids or not self.evidence_ids
        ):
            raise ValueError("asserted feedback requires target and evidence IDs")
        for ids in (self.target_ids, self.evidence_ids):
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate evidence or target ID")
        return self


class FeedbackResult(Contract):
    schema_version: Literal["1"] = "1"
    request_id: Identifier
    snapshot_id: Identifier
    snapshot_sha256: Digest
    run_id: Identifier
    purpose: Literal["feedback"] = "feedback"
    status: Literal["completed", "insufficient_data"]
    items: tuple[FeedbackItem, ...] = Field(default=(), max_length=3)
    missing_sources: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def valid_status(self) -> Self:
        if self.status == "completed" and not self.items:
            raise ValueError("completed feedback requires items")
        if self.status == "insufficient_data" and not self.missing_sources:
            raise ValueError("insufficient data requires missing source descriptions")
        if len({i.item_id for i in self.items}) != len(self.items):
            raise ValueError("duplicate feedback item ID")
        return self

    def validate_scope(
        self,
        request: EvaluationRequest,
        *,
        evidence_ids: set[str],
        item_ids: set[str],
    ) -> None:
        """ID 집합은 모델 출력이 아닌 서버/MCP의 검증된 카탈로그에서 전달한다."""
        if (
            request.purpose != "feedback"
            or self.request_id != request.request_id
            or self.snapshot_id != request.snapshot.snapshot_id
            or self.snapshot_sha256 != request.snapshot.content_sha256
        ):
            raise ValueError("feedback does not belong to this request/snapshot")
        for item in self.items:
            if item.item_id not in item_ids:
                raise ValueError("unknown rubric item")
            if not set(item.target_ids + item.evidence_ids) <= evidence_ids:
                raise ValueError("evidence outside snapshot")
