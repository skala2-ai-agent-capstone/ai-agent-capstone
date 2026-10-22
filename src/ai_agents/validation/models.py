from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from ..shared.models import NonEmpty, StrictModel


class Check(StrictModel):
    path: list[str | int] = Field(min_length=1)
    operation: Literal["equals", "contains", "exists"] = "equals"
    expected: JsonValue = None


class Example(StrictModel):
    case_id: NonEmpty
    category: Literal["normal", "exception", "error", "answerable", "unanswerable", "adversarial"]
    inputs: dict[str, JsonValue]
    expected_description: NonEmpty
    evidence_ids: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)


class Dataset(StrictModel):
    schema_version: Literal["1"] = "1"
    dataset_id: NonEmpty
    version: NonEmpty
    purpose: Literal["feedback", "final", "contract"]
    # 결과 파일에는 입력/출력이 포함된다. 현재 실행기는 가상 데이터만 지원한다.
    data_policy: Literal["synthetic"]
    examples: list[Example] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [example.case_id for example in self.examples]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case IDs")
        return self


class CaseResult(StrictModel):
    case_id: NonEmpty
    category: str
    verdict: Literal["passed", "failed", "unreviewed"]
    expected_description: str
    evidence_ids: list[str]
    actual: dict[str, JsonValue]
    checks: list[dict[str, JsonValue]]
    duration_ms: float
    trace_id: str


class ExperimentResult(StrictModel):
    schema_version: Literal["1"] = "1"
    manifest: dict[str, JsonValue]
    cases: list[CaseResult]
