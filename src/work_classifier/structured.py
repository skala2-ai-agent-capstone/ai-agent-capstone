"""모델 전용 판정 계약과 원문 근거 선택. 공개 ClassificationResult로 안전하게 변환한다."""

import json
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, create_model

from .models import ClassificationResult, Evidence, NonEmpty, StrictModel, WorkType

SELECTION_VERSION = "evidence-selection-v3"
EvidenceIds = Annotated[list[NonEmpty], Field(min_length=1, max_length=4)]


class TaskSelection(StrictModel):
    work_type: WorkType
    goal: NonEmpty
    evidence_ids: EvidenceIds


class SingleDecision(StrictModel):
    status: Literal["classified"]
    task: TaskSelection


class MultipleDecision(StrictModel):
    status: Literal["multiple_tasks"]
    tasks: list[TaskSelection] = Field(min_length=2, max_length=4)

class ReviewDecision(StrictModel):
    status: Literal["needs_review"]
    candidate_types: list[WorkType] = Field(max_length=4)
    missing_information: list[NonEmpty] = Field(min_length=1)


class OutsideDecision(StrictModel):
    status: Literal["out_of_scope"]
    evidence_ids: EvidenceIds


class DecisionEnvelope(StrictModel):
    # 최상위 object 안의 anyOf. 중복 생성하던 work_type/candidate_types를 코드로 파생한다.
    decision: SingleDecision | MultipleDecision | ReviewDecision | OutsideDecision
    reason: NonEmpty


def build_catalog(payload: dict) -> list[dict]:
    sources = []
    for field in ("title", "customer_request"):
        if text := payload.get(field):
            sources.append((f"work:{field}", text, "context"))
    for msg in payload.get("messages", []):
        sources.append((msg["id"], msg["content"], f"message:{msg['role']}"))
    for artifact in payload.get("artifacts", []):
        sources.append((artifact["id"], artifact["content"], artifact.get("stage", "final")))
    catalog = []
    for source_id, content, kind in sources:
        offset = 0
        for line in content.splitlines(keepends=True):
            # 긴 한 줄도 자르되 원문 offset을 유지한다. 생략하거나 요약하지 않는다.
            for start in range(0, len(line), 240):
                piece = line[start:start + 240]
                text = piece.strip()
                if not text:
                    continue
                absolute_start = offset + start + len(piece) - len(piece.lstrip())
                catalog.append({
                    "id": f"e{len(catalog) + 1:04d}", "source_id": source_id,
                    "kind": kind, "start": absolute_start, "end": absolute_start + len(text),
                    "text": text,
                })
            offset += len(line)
    return catalog


def model_payload(payload: str) -> tuple[str, list[dict]]:
    data = json.loads(payload)
    catalog = build_catalog(data)
    # 업무 원문은 유지한다. catalog는 원문과 동일한 구간 및 정확한 위치를 제공한다.
    return json.dumps({**data, "evidence_catalog": catalog}, ensure_ascii=False), catalog


def response_model(catalog: list[dict]) -> type[StrictModel]:
    """실제 입력에 존재하는 비참고자료 근거 ID만 API 스키마에 열거한다."""
    allowed = [entry["id"] for entry in catalog if entry["kind"] != "reference"]
    if not allowed:
        return create_model("DecisionEnvelope", __base__=StrictModel,
                            decision=(ReviewDecision, ...), reason=(NonEmpty, ...))
    if len(allowed) > 1000:
        raise ValueError("근거 후보가 1,000개를 초과했습니다. 입력 범위를 줄여주세요.")
    choices = Enum("EvidenceId", {key: key for key in allowed}, type=str)
    ids = Annotated[list[choices], Field(min_length=1, max_length=4)]
    proof = create_model("TaskSelection", __base__=StrictModel,
                         work_type=(WorkType, ...), goal=(NonEmpty, ...), evidence_ids=(ids, ...))
    single = create_model("SingleDecision", __base__=StrictModel,
                          status=(Literal["classified"], ...), task=(proof, ...))
    multiple = create_model("MultipleDecision", __base__=StrictModel,
                            status=(Literal["multiple_tasks"], ...),
                            tasks=(Annotated[list[proof], Field(min_length=2, max_length=4)], ...))
    outside = create_model("OutsideDecision", __base__=StrictModel,
                           status=(Literal["out_of_scope"], ...), evidence_ids=(ids, ...))
    return create_model("DecisionEnvelope", __base__=StrictModel,
                        decision=(single | multiple | ReviewDecision | outside, ...),
                        reason=(NonEmpty, ...))


def resolve_decision(envelope: DecisionEnvelope, catalog: list[dict]) -> ClassificationResult:
    by_id = {entry["id"]: entry for entry in catalog}
    decision = envelope.decision
    tasks = []
    if isinstance(decision, SingleDecision):
        tasks = [decision.task]
    elif isinstance(decision, MultipleDecision):
        tasks = decision.tasks
    selected = []
    for task in tasks:
        entries = [by_id[key] for key in task.evidence_ids]
        if not any(entry["kind"] != "reference" for entry in entries):
            raise ValueError("참고자료만으로 현재 업무 수행을 입증할 수 없습니다.")
        selected.extend(entries)
    if isinstance(decision, OutsideDecision):
        selected = [by_id[key] for key in decision.evidence_ids]
    evidence = []
    seen = set()
    for entry in selected:
        key = (entry["source_id"], entry["text"])
        if key not in seen:
            evidence.append(Evidence(source_id=key[0], quote=key[1]))
            seen.add(key)
    # 같은 유형의 하위 작업은 유형 분류상 하나다. 서로 다른 유형은 임의로 제거하지 않는다.
    types = list(dict.fromkeys(task.work_type for task in tasks))
    status = decision.status
    reason = envelope.reason
    if isinstance(decision, MultipleDecision) and len(types) == 1:
        status = "classified"
        reason = f"동일 유형({types[0]})의 하위 작업들을 하나의 업무 유형으로 통합했습니다."
    if isinstance(decision, ReviewDecision):
        types = decision.candidate_types
    return ClassificationResult(
        status=status, work_type=types[0] if status == "classified" else None,
        reason=reason, evidence=evidence, candidate_types=types,
        missing_information=decision.missing_information
        if isinstance(decision, ReviewDecision) else [],
    )
