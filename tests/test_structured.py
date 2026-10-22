import json

import pytest
from pydantic import ValidationError

from work_classifier.classification.agent import validate_evidence
from work_classifier.classification.selection import (
    DecisionEnvelope,
    build_catalog,
    resolve_decision,
    response_model,
)
from work_classifier.shared.models import WorkContext


def proof(evidence_ids=None):
    return {"goal": "고객 도입안 작성", "evidence_ids": evidence_ids or ["e0001"]}


def identified(**tasks):
    selections = [{"work_type": name, **item} for name, item in tasks.items()]
    if len(selections) == 1:
        return {"status": "classified", "task": selections[0]}
    return {"status": "multiple_tasks", "tasks": selections}


def envelope(decision):
    return DecisionEnvelope.model_validate({"decision": decision, "reason": "기록의 작업 목적"})


def test_exact_source_offsets_multiline_unicode_and_long_lines():
    content = "  도입 계획 😀 \r\n\n" + "장문 조건 " * 100 + "\n끝"
    context = WorkContext(work_id="x", messages=[
        {"id": "m1", "role": "user", "content": content},
    ])
    catalog = build_catalog(context.llm_payload())
    assert len(catalog) > 3
    for entry in catalog:
        original = context.evidence_sources()[entry["source_id"]]
        assert original[entry["start"]:entry["end"]] == entry["text"]
        assert len(entry["text"]) <= 240
    result = resolve_decision(envelope(identified(proposal=proof([catalog[-1]["id"]]))), catalog)
    validate_evidence(result, context)
    assert result.evidence[0].quote == "끝"


@pytest.mark.parametrize("decision", [
    identified(),
    identified(quotation={"goal": "견적", "evidence_ids": []}),
    {**identified(proposal=proof()), "candidate_types": ["quotation"]},
    {"status": "multiple_tasks", "tasks": [proof()]},
    {"status": "needs_review", "candidate_types": [], "missing_information": []},
    {"status": "out_of_scope", "evidence_ids": []},
])
def test_contradictory_or_incomplete_decisions_rejected(decision):
    with pytest.raises(ValidationError):
        envelope(decision)


def test_genuine_multiple_types_keep_independent_goals_and_evidence():
    context = WorkContext(work_id="x", messages=[
        {"id": "m1", "role": "user", "content": "요구사항 대응표를 작성해줘."},
        {"id": "m2", "role": "user", "content": "그다음 제안서도 작성해줘."},
    ])
    selection = envelope(identified(rfp_analysis=proof(), proposal=proof(["e0002"])))
    result = resolve_decision(selection, build_catalog(context.llm_payload()))
    assert result.status == "multiple_tasks"
    assert result.work_type is None
    assert result.candidate_types == ["rfp_analysis", "proposal"]
    assert {e.source_id for e in result.evidence} == {"m1", "m2"}


def test_duplicate_same_type_tasks_normalize_but_distinct_types_are_preserved():
    catalog = build_catalog({"messages": [
        {"id": "m1", "role": "user", "content": "견적을 작성해줘."},
        {"id": "m2", "role": "assistant", "content": "금액을 수정했습니다."},
    ]})
    selection = envelope({"status": "multiple_tasks", "tasks": [
        {"work_type": "quotation", **proof(["e0001"])},
        {"work_type": "quotation", **proof(["e0002"])},
    ]})
    result = resolve_decision(selection, catalog)
    assert result.status == "classified"
    assert result.work_type == "quotation"
    assert result.candidate_types == ["quotation"]
    assert len(result.evidence) == 2
    assert "통합" in result.reason
    selection.decision.tasks[1].work_type = "proposal"
    result = resolve_decision(selection, catalog)
    assert result.status == "multiple_tasks"
    assert result.candidate_types == ["quotation", "proposal"]


def test_invalid_id_and_reference_only_claim_fail_closed():
    catalog = build_catalog({"artifacts": [
        {"id": "r1", "stage": "reference", "content": "기존 제안서"},
    ]})
    with pytest.raises(KeyError):
        resolve_decision(envelope(identified(proposal=proof(["invented-id"]))), catalog)
    with pytest.raises(ValueError, match="참고자료"):
        resolve_decision(envelope(identified(proposal=proof())), catalog)


def test_review_and_outside_states_do_not_become_single_tasks():
    review = resolve_decision(envelope({
        "status": "needs_review", "candidate_types": ["proposal", "quotation"],
        "missing_information": ["산출물 목적"],
    }), [])
    assert review.status == "needs_review"
    assert review.work_type is None
    assert review.evidence == []
    outside = resolve_decision(envelope({
        "status": "out_of_scope", "evidence_ids": ["e0001"],
    }), build_catalog({"title": "휴가 일정 정리"}))
    assert outside.work_type is None
    assert outside.candidate_types == []


def test_runtime_schema_enforces_exact_allowed_ids_and_excludes_references():
    catalog = build_catalog({"title": "업무", "artifacts": [
        {"id": "r1", "stage": "reference", "content": "참고자료"},
    ]})
    model = response_model(catalog)
    schema = model.model_json_schema()
    assert schema["type"] == "object"
    assert len(schema["properties"]["decision"]["anyOf"]) == 4
    assert schema["$defs"]["EvidenceId"]["enum"] == ["e0001"]
    assert '"quote"' not in json.dumps(schema)
    for invalid in ["unknown", "e0002", "업무...", "work:title"]:
        with pytest.raises(ValidationError):
            model.model_validate({
                "decision": identified(proposal=proof([invalid])), "reason": "판단",
            })
    parsed = model.model_validate({
        "decision": identified(proposal=proof()), "reason": "판단",
    })
    result = resolve_decision(
        DecisionEnvelope.model_validate(parsed.model_dump(mode="json")), catalog,
    )
    assert result.evidence[0].quote == "업무"


def test_reference_only_input_can_only_request_review():
    model = response_model(build_catalog({"artifacts": [
        {"id": "r1", "stage": "reference", "content": "제안서 예시"},
    ]}))
    with pytest.raises(ValidationError):
        model.model_validate({"decision": identified(proposal=proof()), "reason": "판단"})
    model.model_validate({"decision": {
        "status": "needs_review", "candidate_types": [], "missing_information": ["실제 업무 기록"],
    }, "reason": "참고자료만 있음"})
