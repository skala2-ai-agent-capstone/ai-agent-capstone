from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from .models import NonEmpty, StrictModel


class CheckedArtifact(StrictModel):
    """정책 검사를 수행한 산출물."""

    artifact_id: NonEmpty
    version: NonEmpty
    author: NonEmpty


class PolicyCheckScope(StrictModel):
    """어떤 대화와 산출물을 검사했는지."""

    user_message_ids: list[NonEmpty]
    artifact_ref: CheckedArtifact
    note: NonEmpty


class PolicyViolation(StrictModel):
    """서버가 탐지한 정책 위반 한 건."""

    rule_id: NonEmpty
    clause: NonEmpty
    location: NonEmpty
    matched_customer_id: NonEmpty | None = None


class MaskingCount(StrictModel):
    """자료에서 마스킹된 정보의 집계."""

    rule_id: NonEmpty
    source: NonEmpty
    count: int = Field(ge=0, strict=True)


class PolicyCheckResult(StrictModel):
    """business MCP가 반환하는 정책 검사 결과의 data 부분."""

    work_id: NonEmpty
    version: NonEmpty
    evidence_id: NonEmpty
    checked_at: datetime
    scope: PolicyCheckScope
    violations: list[PolicyViolation]
    masking: list[MaskingCount]

class RegistrationRecord(StrictModel):
    """직원이 업무를 등록할 때 입력한 내용."""

    model_config = ConfigDict(str_strip_whitespace=False)

    evidence_id: NonEmpty
    content: dict[str, JsonValue] | str


class SourceLine(StrictModel):
    """고객 원문의 한 줄과 그 줄의 근거 ID."""

    model_config = ConfigDict(str_strip_whitespace=False)

    line_id: NonEmpty
    text: str


class CustomerSourceMessage(StrictModel):
    """고객에게 받은 원문. 직원의 AI 지시와 구분한다."""

    model_config = ConfigDict(str_strip_whitespace=False)

    evidence_id: NonEmpty
    channel: str | None
    received_at: datetime | None
    content: str
    content_type: Literal["data"]
    lines: list[SourceLine]

class ArtifactSpan(StrictModel):
    """산출물의 한 구간과 해당 구간의 근거 ID."""

    model_config = ConfigDict(str_strip_whitespace=False)

    span_id: NonEmpty
    text: str


class ArtifactRecord(StrictModel):
    """business MCP에서 조회한 산출물 한 버전."""

    model_config = ConfigDict(str_strip_whitespace=False)

    artifact_id: NonEmpty
    work_id: NonEmpty
    version: NonEmpty
    author: NonEmpty
    saved_at: datetime
    is_evaluation_target: bool = Field(strict=True)
    content: str
    spans: list[ArtifactSpan]