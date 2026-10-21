from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmpty = Annotated[str, Field(min_length=1)]
WorkType = Literal["rfp_analysis", "proposal", "quotation", "customer_response"]
ClassificationStatus = Literal[
    "classified", "needs_review", "multiple_tasks", "out_of_scope"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Message(StrictModel):
    id: NonEmpty
    role: Literal["user", "assistant", "system", "tool"]
    content: NonEmpty


class Artifact(StrictModel):
    # 각 버전마다 고유한 입력 ID를 부여합니다. 실제 문서 ID와의 매핑은 어댑터의 책임입니다.
    id: NonEmpty
    version: int = Field(default=1, ge=1, strict=True)
    title: str = ""
    stage: Literal["draft", "final", "reference"] = "final"
    content: NonEmpty


class WorkContext(StrictModel):
    """Agent 내부 계약. MCP 응답은 추후 이 모델로 변환합니다."""

    work_id: NonEmpty
    title: str = ""
    customer_request: str = ""
    messages: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    missing_sources: list[NonEmpty] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        ids = [m.id for m in self.messages] + [a.id for a in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("메시지·산출물 ID는 입력 전체에서 고유해야 합니다.")
        if {"work:title", "work:customer_request"} & set(ids):
            raise ValueError("work:title / work:customer_request는 예약된 근거 ID입니다.")
        if not (self.title or self.customer_request or self.messages or self.artifacts):
            raise ValueError("분류할 제목·고객 요청·대화·산출물 중 하나 이상이 필요합니다.")
        if self.coverage == "complete" and self.missing_sources:
            raise ValueError("수집 범위가 complete이면 missing_sources는 비어 있어야 합니다.")
        return self

    def evidence_sources(self) -> dict[str, str]:
        """검증용 원문 매핑. work_id·파일명·테스트 정답은 근거로 사용하지 않습니다."""
        sources = {m.id: m.content for m in self.messages}
        sources.update({a.id: a.content for a in self.artifacts})
        if self.title:
            sources["work:title"] = self.title
        if self.customer_request:
            sources["work:customer_request"] = self.customer_request
        return sources

    def llm_payload(self) -> dict:
        data = self.model_dump(mode="json")
        # 업무 ID/샘플 파일명에 유형이 드러나더라도 분류 힌트로 쓰지 않습니다.
        data.pop("work_id")
        data["metadata_evidence_ids"] = {
            "title": "work:title",
            "customer_request": "work:customer_request",
        }
        return data


class Evidence(StrictModel):
    source_id: NonEmpty
    quote: NonEmpty = Field(description="입력 원문에서 그대로 복사한 짧은 연속 구간")


class ClassificationResult(StrictModel):
    # 공개 결과 계약. 모델 전용 선택 스키마는 structured.DecisionEnvelope에 정의합니다.
    status: ClassificationStatus
    work_type: WorkType | None
    reason: NonEmpty
    evidence: list[Evidence]
    candidate_types: list[WorkType]
    missing_information: list[NonEmpty]

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if len(self.candidate_types) != len(set(self.candidate_types)):
            raise ValueError("candidate_types에는 중복 유형을 넣을 수 없습니다.")
        if self.status == "classified":
            if self.work_type is None:
                raise ValueError("classified에는 work_type이 필요합니다.")
            if self.candidate_types != [self.work_type]:
                raise ValueError("classified의 candidate_types는 선택한 유형 하나여야 합니다.")
        else:
            if self.work_type is not None:
                raise ValueError("classified 이외 상태의 work_type은 null이어야 합니다.")
            if self.status == "multiple_tasks" and len(self.candidate_types) < 2:
                raise ValueError("multiple_tasks에는 두 개 이상의 유형이 필요합니다.")
            if self.status == "out_of_scope" and self.candidate_types:
                raise ValueError("out_of_scope의 candidate_types는 비어 있어야 합니다.")
        if self.status != "needs_review" and not self.evidence:
            raise ValueError("확정 분류·복합 업무·범위 밖 판단에는 근거가 필요합니다.")
        if self.status == "needs_review" and not self.missing_information:
            raise ValueError("needs_review에는 필요한 정보 또는 해소할 모호성을 적어야 합니다.")
        return self


class ClassificationReport(StrictModel):
    work_id: NonEmpty
    input_sha256: NonEmpty
    model: NonEmpty
    prompt_version: NonEmpty
    response_id: str | None
    classification: ClassificationResult
    decision_trace: dict | None = None

