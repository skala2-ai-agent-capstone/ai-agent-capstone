"""생성 응답 스키마와 최종 검증이 같은 5개 항목 규칙을 쓰는지 대조한다."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_evaluation_agent import ToolFixture, collect, selected_result, valid_result
from test_evaluation_selection import catalogs
from test_mcp_adapter import CASES

from ai_agents.evaluation_agent.final.contract import ITEM_KEYS, MISSING_WORK_MESSAGES
from ai_agents.evaluation_agent.final.selection import (
    choice_requirements,
    resolve_selection,
    selection_model,
)
from ai_agents.evaluation_agent.final.validation import EvaluationError, validate_evaluation_result
from ai_agents.shared.evidence_models import PolicyViolation
from ai_agents.shared.models import Evidence
from ai_agents.shared.workflow_models import content_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_all_40_cases_wrong_primary_source_fails_for_each_of_five_criteria(case):
    fixture = ToolFixture(case=case)
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    public = valid_result(fixture, evidence, materials)
    good = selected_result(public, payload)
    model = selection_model(choices, fixture.request)
    validator = Draft202012Validator(model.model_json_schema())
    assert validator.is_valid(good)
    for index, key in enumerate(ITEM_KEYS):
        row = good["items"][index]
        requirement = payload["criterion_requirements"][row["criterion_id"]]
        assert requirement["can_score"]
        # 이번 항목의 필수 근거 종류를 충족하지 않는 실제 입력 구간을 선택한다.
        wrong = next(
            choice for choice in choices.work
            if choice["id"] not in requirement["required_work_choice_ids"]
        )
        broken = deepcopy(good)
        broken["items"][index]["required_work_evidence_id"] = wrong["id"]
        assert not validator.is_valid(broken), key
        with pytest.raises(ValidationError) as invalid:
            model.model_validate(broken)
        assert invalid.value.errors()[0]["loc"] == ("items", index)
        assert MISSING_WORK_MESSAGES[key] in str(invalid.value)

        # 공개 결과를 직접 전달해도 같은 규칙으로 거부해야 한다.
        unsupported = public.model_copy(deep=True)
        unsupported.items[index].work_evidence = [Evidence(
            source_id=wrong["source_id"], quote=wrong["text"],
        )]
        with pytest.raises(EvaluationError) as invalid_public:
            validate_evaluation_result(
                fixture.request, unsupported, evidence=evidence, materials=materials,
            )
        assert str(invalid_public.value) == MISSING_WORK_MESSAGES[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5), ids=ITEM_KEYS)
async def test_missing_required_kind_disables_scoring_instead_of_substituting_other_data(index):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    criterion_id = wire["items"][index]["criterion_id"]
    unavailable = set(payload["criterion_requirements"][criterion_id]["required_work_choice_ids"])
    choices.work = [row for row in choices.work if row["id"] not in unavailable]
    requirements = choice_requirements(choices, fixture.request)
    assert not requirements[criterion_id]["can_score"]
    assert requirements[criterion_id]["required_work_choice_ids"] == []
    model = selection_model(choices, fixture.request)
    validator = Draft202012Validator(model.model_json_schema())
    available = {row["id"]: row["text"] for row in choices.work}
    for row in wire["items"]:
        eligible = requirements[row["criterion_id"]]["required_work_choice_ids"]
        primary = eligible[0] if eligible else None
        row.update(
            observation="insufficient_data", score=None, required_work_evidence_id=primary,
            work_evidence_ids=[],
            reason=f"「{available[primary][:80]}」 추가 자료가 필요합니다." if primary
            else "[근거 없음] 해당 항목의 업무 자료가 없습니다.",
        )
    assert validator.is_valid(wire)
    model.model_validate(wire)
    broken = deepcopy(wire)
    broken["items"][index].update(
        observation="observed", score=0,
        required_work_evidence_id=choices.work[0]["id"] if choices.work else None,
    )
    assert not validator.is_valid(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5), ids=ITEM_KEYS)
async def test_wrong_primary_rule_basis_is_rejected_for_every_item(index):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    # 조회 가능한 비교 자료라도 승인된 항목의 기준 근거를 대체할 수 없다.
    choices.rules.append({
        "id": "r-comparison", "document_id": "COMPARISON", "version": "v1",
        "section_id": "S1", "content": "비교 자료", "start": 0, "end": 5,
    })
    wire["items"][index]["required_rule_evidence_id"] = "r-comparison"
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="승인된 기준 근거를 선택"):
        model.model_validate(wire)


@pytest.mark.asyncio
async def test_policy_violation_score_rule_is_identical_in_schema_and_final_validation():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    evidence.policy_check.violations = [PolicyViolation(
        rule_id="V-TEST", clause="ART-07",
        location=evidence.policy_check.scope.user_message_ids[0],
    )]
    materials.evidence_sha256 = content_hash(evidence)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    public = valid_result(fixture, evidence, materials)
    wire = selected_result(public, payload)
    security = wire["items"][3]
    assert payload["criterion_requirements"][security["criterion_id"]]["allowed_scores"] == [0]
    checked = validate_evaluation_result(
        fixture.request, resolve_selection(wire, choices, fixture.request),
        evidence=evidence, materials=materials,
    )
    assert checked.items[3].score == 0
    security["score"] = 2
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="정책 위반"):
        model.model_validate(wire)
    public.items[3].score = 2
    with pytest.raises(EvaluationError, match="정책 위반"):
        validate_evaluation_result(fixture.request, public, evidence=evidence, materials=materials)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [2, 4], ids=["verification", "quality"])
async def test_missing_quote_disables_two_points_in_both_schema_and_final_validation(index):
    fixture = ToolFixture("quote")
    original = fixture.business

    async def no_quote(name, arguments):
        if name == "compute_reference_quote":
            return {"ok": False, "error": {
                "code": "NOT_FOUND", "message": "기준 견적 없음", "retryable": False,
            }}
        return await original(name, arguments)

    fixture.business = no_quote
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    public = valid_result(fixture, evidence, materials)
    wire = selected_result(public, payload)
    criterion_id = wire["items"][index]["criterion_id"]
    assert 2 not in payload["criterion_requirements"][criterion_id]["allowed_scores"]
    validate_evaluation_result(
        fixture.request, resolve_selection(wire, choices, fixture.request),
        evidence=evidence, materials=materials,
    )
    wire["items"][index]["score"] = 2
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="기준 견적 조회"):
        model.model_validate(wire)
    public.items[index].score = 2
    with pytest.raises(EvaluationError, match="기준 견적 조회"):
        validate_evaluation_result(fixture.request, public, evidence=evidence, materials=materials)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["items", "coaching"])
async def test_duplicate_or_missing_item_is_not_accepted_as_a_complete_report(name):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    wire[name][4] = deepcopy(wire[name][0])
    with pytest.raises(EvaluationError, match="누락 또는 중복"):
        resolve_selection(wire, choices, fixture.request)


@pytest.mark.asyncio
async def test_conflicting_approved_scores_request_review_instead_of_inventing_zero():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    evidence.policy_check.violations = [PolicyViolation(
        rule_id="V-TEST", clause="ART-07",
        location=evidence.policy_check.scope.user_message_ids[0],
    )]
    materials.evidence_sha256 = content_hash(evidence)
    # 서버가 위반을 확인했는데 승인 기준에 2점만 있는 잘못된 조건을 가정한다.
    criterion = fixture.request.approved_criteria.items[3]
    criterion.levels = [level for level in criterion.levels if level.score == 2]
    fixture.request.approval.criteria_sha256 = content_hash(fixture.request.approved_criteria)
    choices, _, _, _ = catalogs(fixture, evidence, materials)
    requirement = choice_requirements(choices, fixture.request)[criterion.id]
    assert requirement["required_work_choice_ids"]
    assert requirement["allowed_scores"] == []
    assert not requirement["can_score"]
    assert requirement["review_reason"]
    assert 0 not in {level.score for level in criterion.levels}


@pytest.mark.asyncio
async def test_same_document_key_outside_approved_quote_cannot_replace_item_basis():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    public = valid_result(fixture, evidence, materials)
    original = fixture.request.approved_criteria.references[0]
    original.content += "\n승인된 항목 근거 밖의 조항"
    for ref in materials.references:
        if (ref.document_id, ref.version, ref.section_id) == (
            original.document_id, original.version, original.section_id,
        ):
            ref.content = original.content
    fixture.request.approval.criteria_sha256 = content_hash(fixture.request.approved_criteria)
    public.criteria_sha256 = fixture.request.approval.criteria_sha256
    public.items[4].rule_evidence[0].quote = "승인된 항목 근거 밖의 조항"
    with pytest.raises(EvaluationError, match="승인된 기준 근거가 없습니다"):
        validate_evaluation_result(fixture.request, public, evidence=evidence, materials=materials)
