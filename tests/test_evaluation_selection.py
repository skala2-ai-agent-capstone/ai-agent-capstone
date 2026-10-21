import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_evaluation_agent import ToolFixture, collect, selected_result, valid_result
from test_mcp_adapter import CASES

from work_classifier.evaluation_selection import (
    build_choices,
    choice_requirements,
    pieces,
    resolve_selection,
    security_choice_ids,
    selection_model,
)
from work_classifier.evaluation_validation import evidence_catalog, validate_evaluation_result


def catalogs(fixture, evidence, materials):
    sources, kinds = evidence_catalog(fixture.request, evidence, materials)
    references = {
        (r.document_id, r.version, r.section_id): r
        for r in fixture.request.approved_criteria.references + materials.references
    }
    choices = build_choices(fixture.request, sources, kinds, references, evidence=evidence)
    payload = {
        "work_evidence_catalog": choices.work, "reference_catalog": choices.rules,
        "criterion_requirements": choice_requirements(choices, fixture.request),
    }
    return choices, payload, sources, references


def test_piece_offsets_preserve_unicode_crlf_spacing_and_long_lines():
    text = "  고객 요구\r\n\t필수  항목을 확인해 주세요.\n" + "한" * 1001
    entries = list(pieces(text))
    assert len(entries) == 5
    assert all(quote == text[start:end] for quote, start, end in entries)
    assert "  " in entries[1][0]
    assert "".join(quote for quote, _, _ in entries[2:]) == "한" * 1001


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_all_40_cases_keep_source_identity_and_exact_original_segments(case):
    fixture = ToolFixture(case=case)
    evidence, materials = await collect(fixture)
    choices, payload, sources, references = catalogs(fixture, evidence, materials)
    for row in choices.work:
        assert row["text"] == sources[row["source_id"]][row["start"] : row["end"]]
    for row in choices.rules:
        key = row["document_id"], row["version"], row["section_id"]
        assert row["content"] == references[key].content[row["start"] : row["end"]]
    public = valid_result(fixture, evidence, materials)
    wire = selected_result(public, payload)
    assert all("quote" not in json.dumps(item) for item in wire["items"])
    parsed = selection_model(choices, fixture.request).model_validate(wire)
    result = resolve_selection(parsed.model_dump(mode="json"), choices, fixture.request)
    checked = validate_evaluation_result(
        fixture.request, result, evidence=evidence, materials=materials
    )
    assert len(checked.items) == 5
    assert checked.run_id == fixture.request.run_id
    assert checked.criteria_sha256 == fixture.request.approval.criteria_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["work_id", "rule_id", "quote", "run_id", "criterion"])
async def test_unknown_ids_and_model_written_quotes_cannot_enter_selection_result(invalid):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    if invalid == "work_id":
        wire["items"][0]["required_work_evidence_id"] = "w-does-not-exist"
    elif invalid == "rule_id":
        wire["items"][0]["required_rule_evidence_id"] = "r-does-not-exist"
    elif invalid == "quote":
        wire["items"][0]["quote"] = "원문을 바꿔 쓴 인용문"
    elif invalid == "run_id":
        wire["run_id"] = "다른 실행"
    else:
        wire["items"][0]["criterion_id"] = "unknown.criterion"
    with pytest.raises(ValidationError):
        resolve_selection(wire, choices, fixture.request)


@pytest.mark.asyncio
async def test_selected_ai_answer_is_not_employee_behavior_evidence():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    assistant = next(row["id"] for row in choices.work if row["kind"] == "message:assistant")
    wire["items"][0]["required_work_evidence_id"] = assistant
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="직원의 실제 행동 근거"):
        model.model_validate(wire)


@pytest.mark.asyncio
async def test_unobserved_item_stays_without_score_or_fabricated_work_citation():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    wire["items"][0].update(
        observation="unobserved", score=None, required_work_evidence_id=None, work_evidence_ids=[],
    )
    result = resolve_selection(wire, choices, fixture.request)
    checked = validate_evaluation_result(
        fixture.request, result, evidence=evidence, materials=materials
    )
    assert checked.items[0].score is None
    assert checked.items[0].work_evidence == []
    assert "4개 채점" in checked.summary


@pytest.mark.asyncio
async def test_model_schema_only_enumerates_current_snapshot_candidates():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, _, _, _ = catalogs(fixture, evidence, materials)
    schema = selection_model(choices, fixture.request).model_json_schema()
    assert set(schema["$defs"]["EvaluationWorkChoice"]["enum"]) == {
        row["id"] for row in choices.work
    }
    assert set(schema["$defs"]["EvaluationRuleChoice"]["enum"]) == {
        row["id"] for row in choices.rules
    }
    assert "anyOf" in schema["properties"]["items"]["items"]
    for branch in schema["properties"]["items"]["items"]["anyOf"]:
        fields = schema["$defs"][branch["$ref"].rsplit("/", 1)[1]]["properties"]
        assert "work_evidence_ids" in fields and "rule_evidence_ids" in fields
        assert "quote" not in fields and "work_evidence" not in fields


@pytest.mark.asyncio
async def test_repeated_identical_selection_is_one_original_citation_without_new_evidence():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    expected = wire["items"][0]["required_work_evidence_id"]
    wire["items"][0]["work_evidence_ids"] = [expected, expected]
    result = resolve_selection(wire, choices, fixture.request)
    assert len(result.items[0].work_evidence) == 1
    original = next(row for row in choices.work if row["id"] == expected)
    assert result.items[0].work_evidence[0].source_id == original["source_id"]
    assert result.items[0].work_evidence[0].quote == original["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"score": None},
    {"required_work_evidence_id": None},
    {"score": None, "required_work_evidence_id": None, "work_evidence_ids": []},
    {"observation": "unobserved"},
    {"observation": "insufficient_data"},
    {"observation": "needs_review", "score": None, "review_required": False},
    {"score": 1},  # 보안은 0·2점만 가능하다.
    {"score": -1},
    {"score": 3},
])
async def test_contradictory_outputs_are_rejected_by_both_api_schema_and_parser(change):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    wire["items"][3].update(change)
    model = selection_model(choices, fixture.request)
    # Python 사후 검증만 강화한 것이 아니라 API로 보낼 JSON Schema 자체도 거부해야 한다.
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError) as caught:
        model.model_validate(wire)
    assert caught.value.errors()[0]["loc"] == ("items", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [False, True, 0.0, 2.0, "0", "2"])
async def test_scores_are_not_silently_coerced_to_integers(score):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    wire["items"][3]["score"] = score
    with pytest.raises(ValidationError, match="점수는 정수"):
        selection_model(choices, fixture.request).model_validate(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unobserved", "insufficient_data", "needs_review"])
async def test_unscored_states_remain_valid_with_null_score(state):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    wire["items"][3].update(
        observation=state, score=None, required_work_evidence_id=None, work_evidence_ids=[],
        review_required=state == "needs_review",
    )
    model = selection_model(choices, fixture.request)
    assert Draft202012Validator(model.model_json_schema()).is_valid(wire)
    result = resolve_selection(wire, choices, fixture.request)
    checked = validate_evaluation_result(
        fixture.request, result, evidence=evidence, materials=materials,
    )
    assert checked.items[3].score is None
    assert checked.items[3].observation == state


@pytest.mark.asyncio
async def test_no_work_choices_cannot_produce_scored_results_or_fabricated_ids():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    choices.work = []
    model = selection_model(choices, fixture.request)
    for item in wire["items"]:
        item.update(
            observation="insufficient_data", score=None, required_work_evidence_id=None,
            work_evidence_ids=[],
        )
    assert Draft202012Validator(model.model_json_schema()).is_valid(wire)
    model.model_validate(wire)
    broken = deepcopy(wire)
    broken["items"][0].update(observation="observed", score=0)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(broken)
    with pytest.raises(ValidationError):
        model.model_validate(broken)


@pytest.mark.asyncio
async def test_observed_zero_keeps_its_reason_and_original_reviewed_artifact():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, sources, _ = catalogs(fixture, evidence, materials)
    public = valid_result(fixture, evidence, materials)
    # 실제 모델 판단을 흉내 내는 고정 응답. 누락 판정의 정확도를 시험하는 것은 아니다.
    public.items[4].reason = "검토한 산출물이 승인된 필수 조건을 충족하지 못해 0점입니다."
    wire = selected_result(public, payload)
    result = resolve_selection(wire, choices, fixture.request)
    checked = validate_evaluation_result(
        fixture.request, result, evidence=evidence, materials=materials,
    )
    item = checked.items[4]
    assert item.score == 0
    assert item.observation == "observed"
    assert item.reason == public.items[4].reason
    assert all(proof.quote in sources[proof.source_id] for proof in item.work_evidence)


@pytest.mark.asyncio
async def test_score_must_be_allowed_by_the_approved_criterion():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    # 이 기준은 0·2만 허용한다. 일반 항목이어도 새로 1점을 허용하지 않는다.
    wire["items"][0]["score"] = 1
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="승인된 기준에서 허용하지 않는 점수"):
        model.model_validate(wire)


@pytest.mark.asyncio
async def test_invalid_diagnostics_only_include_safe_selection_fields():
    from work_classifier.evaluation_agent import validation_feedback

    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    row = wire["items"][3]
    row.update(
        score=None, required_work_evidence_id=None, work_evidence_ids=[],
        reason="private-customer-text",
    )
    with pytest.raises(ValidationError) as caught:
        selection_model(choices, fixture.request).model_validate(wire)
    feedback = validation_feedback(caught.value)
    assert '"criterion_id": "proposal.security"' in feedback
    assert '"observation": "observed"' in feedback
    assert '"score": null' in feedback
    assert '"work_evidence_ids": []' in feedback
    assert "private-customer-text" not in feedback
    row.update(criterion_id="secret-id", work_evidence_ids=["secret-proof"])
    with pytest.raises(ValidationError) as caught:
        selection_model(choices, fixture.request).model_validate(wire)
    feedback = validation_feedback(caught.value)
    assert "secret-id" not in feedback and "secret-proof" not in feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message:user", "target_artifact"])
async def test_security_cannot_substitute_general_primary_work_for_server_policy(kind):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    wire = selected_result(valid_result(fixture, evidence, materials), payload)
    policy_ids = security_choice_ids(choices, fixture.context.work_id)
    assert policy_ids
    wrong = next(row["id"] for row in choices.work if row["kind"] == kind)
    wire["items"][3]["required_work_evidence_id"] = wrong
    model = selection_model(choices, fixture.request)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(wire)
    with pytest.raises(ValidationError, match="서버 정책 검사 근거가 필요"):
        model.model_validate(wire)

    # 필수 서버 근거가 있으면 대화·산출물을 추가 비교 근거로 함께 인용할 수 있다.
    wire["items"][3]["required_work_evidence_id"] = policy_ids[0]
    wire["items"][3]["work_evidence_ids"] = [wrong]
    assert Draft202012Validator(model.model_json_schema()).is_valid(wire)
    result = resolve_selection(wire, choices, fixture.request)
    validate_evaluation_result(fixture.request, result, evidence=evidence, materials=materials)


@pytest.mark.asyncio
async def test_security_without_policy_can_only_return_an_unscored_result():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    public = valid_result(fixture, evidence, materials)
    public.items[3].observation = "insufficient_data"
    public.items[3].score = None
    public.items[3].work_evidence = []
    evidence.policy_check = None
    from work_classifier.workflow_models import content_hash

    materials.evidence_sha256 = content_hash(evidence)
    choices, payload, _, _ = catalogs(fixture, evidence, materials)
    assert security_choice_ids(choices, fixture.context.work_id) == []
    wire = selected_result(public, payload)
    model = selection_model(choices, fixture.request)
    assert Draft202012Validator(model.model_json_schema()).is_valid(wire)
    result = resolve_selection(wire, choices, fixture.request)
    checked = validate_evaluation_result(
        fixture.request, result, evidence=evidence, materials=materials,
    )
    assert checked.items[3].score is None
    broken = deepcopy(wire)
    broken["items"][3].update(
        observation="observed", score=0, required_work_evidence_id=choices.work[0]["id"],
    )
    assert not Draft202012Validator(model.model_json_schema()).is_valid(broken)
    with pytest.raises(ValidationError, match="서버 정책 검사 근거가 필요"):
        model.model_validate(broken)


@pytest.mark.asyncio
async def test_security_only_accepts_policy_sections_from_the_same_work():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    choices, _, _, _ = catalogs(fixture, evidence, materials)
    expected = security_choice_ids(choices, fixture.context.work_id)
    choices.work.append({
        "id": "w-other-work", "kind": "policy_check", "source_id": "OTHER:policy_check",
        "text": "다른 업무의 검사 결과", "start": 0, "end": 12,
    })
    assert security_choice_ids(choices, fixture.context.work_id) == expected
    schema = selection_model(choices, fixture.request).model_json_schema()
    allowed = schema["$defs"]["RequiredWorkChoice3"]
    assert set(allowed.get("enum", [allowed.get("const")])) == set(expected)
