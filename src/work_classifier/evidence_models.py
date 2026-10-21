import json
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

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


class ArtifactReference(StrictModel):
    artifact_id: NonEmpty
    version: NonEmpty
    author: NonEmpty
    saved_at: datetime
    is_evaluation_target: bool = Field(strict=True)


class WorkCaseEvidence(StrictModel):
    """분류 정답 등 불필요한 필드는 제외한 업무 조회 결과."""

    work_id: NonEmpty
    registration: RegistrationRecord | None
    source_message: CustomerSourceMessage | None
    artifact_refs: list[ArtifactReference] = Field(min_length=1)
    source_documents: list[dict[str, JsonValue]] | None = None

    @model_validator(mode="after")
    def check_source_ids(self) -> Self:
        if (
            self.registration is not None
            and self.registration.evidence_id != f"{self.work_id}:registration"
        ):
            raise ValueError("등록 내용의 근거 ID가 업무와 다릅니다.")
        message = self.source_message
        if message is not None:
            if message.evidence_id != f"{self.work_id}:source_message":
                raise ValueError("고객 원문의 근거 ID가 업무와 다릅니다.")
            expected = [
                (f"{self.work_id}:source_message:L{i:02d}", text)
                for i, text in enumerate(message.content.split("\n"), 1)
            ]
            if [(line.line_id, line.text) for line in message.lines] != expected:
                raise ValueError("고객 원문의 줄 ID 또는 내용이 전체 원문과 다릅니다.")
        return self


class SnapshotArtifact(StrictModel):
    artifact_id: NonEmpty
    version: NonEmpty
    saved_at: datetime


class EvaluationRun(StrictModel):
    """토큰 발급 응답에서 전달받는 실행 범위. LLM이 생성하지 않는다."""

    run_id: NonEmpty
    work_id: NonEmpty
    snapshot: dict[str, JsonValue]

    @property
    def target(self) -> SnapshotArtifact:
        return SnapshotArtifact.model_validate(self.snapshot.get("artifact_ref"))

    @model_validator(mode="after")
    def check_snapshot(self) -> Self:
        target = self.target
        if target.saved_at.utcoffset() is None:
            raise ValueError("스냅샷 저장 시각에는 시간대가 필요합니다.")
        for key in ("reference_refs", "linked_doc_refs"):
            refs = self.snapshot.get(key)
            if not isinstance(refs, list) or any(
                not isinstance(ref, dict) or not ref.get("doc_id") or not ref.get("version")
                for ref in refs
            ):
                raise ValueError(f"스냅샷의 {key} 목록이 올바르지 않습니다.")
        bounds = self.snapshot.get("conversation_range")
        if not isinstance(bounds, dict):
            raise ValueError("스냅샷 대화 범위가 필요합니다.")
        start, end = bounds.get("from_seq"), bounds.get("to_seq")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
            or (start == 0 and end != 0)
        ):
            raise ValueError("스냅샷 대화 범위가 올바르지 않습니다.")
        return self


class EvaluationEvidence(StrictModel):
    """같은 실행에서 조회한 B 전용 추가 근거. A의 공통 입력과 별도로 전달한다."""

    context: EvaluationRun
    work_case: WorkCaseEvidence
    artifacts: list[ArtifactRecord] = Field(min_length=1)
    policy_check: PolicyCheckResult | None

    @model_validator(mode="after")
    def check_consistency(self) -> Self:
        context = self.context
        target = context.target
        if self.work_case.work_id != context.work_id:
            raise ValueError("다른 업무의 등록 정보입니다.")
        refs = {(r.artifact_id, r.version): r for r in self.work_case.artifact_refs}
        if len(refs) != len(self.work_case.artifact_refs):
            raise ValueError("산출물 참조가 중복되었습니다.")
        records = {(a.artifact_id, a.version): a for a in self.artifacts}
        if len(records) != len(self.artifacts) or records.keys() != refs.keys():
            raise ValueError("조회한 산출물은 업무의 허용 버전 목록과 정확히 일치해야 합니다.")
        targets = []
        span_ids = []
        for key, artifact in records.items():
            ref = refs[key]
            if artifact.work_id != context.work_id:
                raise ValueError("다른 업무의 산출물입니다.")
            if artifact.saved_at.utcoffset() is None or artifact.saved_at > target.saved_at:
                raise ValueError("스냅샷 이후 산출물이거나 저장 시각에 시간대가 없습니다.")
            if (artifact.author, artifact.saved_at, artifact.is_evaluation_target) != (
                ref.author,
                ref.saved_at,
                ref.is_evaluation_target,
            ):
                raise ValueError("산출물 본문과 참조 정보가 일치하지 않습니다.")
            expected_target = key == (target.artifact_id, target.version)
            if artifact.is_evaluation_target != expected_target:
                raise ValueError("산출물의 평가 대상 표시가 스냅샷과 다릅니다.")
            if expected_target:
                targets.append(artifact)
                if artifact.saved_at != target.saved_at:
                    raise ValueError("평가 대상 산출물의 저장 시각이 스냅샷과 다릅니다.")
            for span in artifact.spans:
                if not span.span_id.startswith(f"{artifact.artifact_id}:{artifact.version}:"):
                    raise ValueError("산출물 구간 ID의 문서 또는 버전이 다릅니다.")
                span_ids.append(span.span_id)
        if len(targets) != 1 or len(span_ids) != len(set(span_ids)):
            raise ValueError("평가 대상 산출물이 없거나 구간 ID가 중복되었습니다.")
        policy = self.policy_check
        if policy is not None:
            checked = policy.scope.artifact_ref
            if (
                policy.work_id != context.work_id
                or policy.version != target.version
                or policy.evidence_id != f"{context.work_id}:policy_check"
                or (checked.artifact_id, checked.version, checked.author)
                != (target.artifact_id, target.version, targets[0].author)
            ):
                raise ValueError("정책 검사 결과의 업무·산출물·버전이 다릅니다.")
            locations = set(policy.scope.user_message_ids) | {s.span_id for s in targets[0].spans}
            if not targets[0].spans:
                locations.add(f"{target.artifact_id}:{target.version}")
            if any(v.location not in locations for v in policy.violations):
                raise ValueError("정책 위반 위치가 검사 범위에 없습니다.")
        return self

    @property
    def missing_sources(self) -> list[str]:
        # 조회하지 않은 보안 결과를 위반 없음으로 바꾸지 않는다.
        return ["policy_check"] if self.policy_check is None else []

    def evidence_sources(self) -> dict[str, str]:
        """서버가 발급한 근거 ID와 마스킹된 내용을 보존한다."""
        sources = {}
        registration = self.work_case.registration
        if registration is not None:
            content = registration.content
            sources[registration.evidence_id] = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False, sort_keys=True)
            )
        message = self.work_case.source_message
        if message is not None:
            sources[message.evidence_id] = message.content
            sources.update({line.line_id: line.text for line in message.lines})
        for artifact in self.artifacts:
            sources.update({span.span_id: span.text for span in artifact.spans})
        if self.policy_check is not None:
            sources[self.policy_check.evidence_id] = self.policy_check.model_dump_json()
        return sources
