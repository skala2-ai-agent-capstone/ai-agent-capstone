import json

import pytest
from test_complete_workflow import client_fixture
from test_stored_final_evaluation import JOB, StoredGateway, workflow

from ai_agents.evaluation_agent.final.validation import EvaluationError
from ai_agents.orchestration.final_evaluation import FinalCheckpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_stored_payload_identifies_collected_target_instead_of_empty_metadata(
    tmp_path, work_type,
):
    gateway = StoredGateway(work_type)
    run, client = workflow(gateway, tmp_path)
    await run.run(JOB)
    payload = json.loads(client.responses.parse.call_args.kwargs["input"][1]["content"])
    prepared = FinalCheckpoint(tmp_path, JOB).read()
    target = next(a.model_dump(mode="json") for a in prepared.evidence.artifacts
                  if a.is_evaluation_target)
    assert "artifacts" not in payload["work_record"]
    assert "messages" not in payload["work_record"]
    facts = payload["collected_evidence_facts"]
    assert facts["target_artifact"] == {
        "artifact_id": target["artifact_id"], "version": target["version"],
        "saved_at": target["saved_at"], "author": target["author"],
        "content_present": bool(target["content"].strip()),
        "span_ids": [s["span_id"] for s in target["spans"]],
    }
    assert facts["policy_check_available"] is True
    assert any(row["kind"] == "target_artifact" for row in payload["work_evidence_catalog"])
    assert next(iter(payload)) == "work_evidence_catalog"
    assert "evidence" not in payload and "work_evidence_sources" not in payload
    assert "messages" not in payload["additional_details"]
    assert payload["additional_details"]["artifact_diffs"] == prepared.materials.details[
        "artifact_diffs"
    ]
    assert payload["additional_details"]["reference_status"] == prepared.materials.details[
        "reference_status"
    ]
    assert {item["id"] for item in payload["criteria"]} == {
        item.id for item in prepared.request.approved_criteria.items
    }
    definitions = {row.item_id: row.criterion for row in prepared.request.stored_rubric.items}
    item_ids = prepared.request.stored_rubric.item_ids()
    assert all(item["criterion"] == definitions[item_ids[item["id"]]]
               for item in payload["criteria"])
    sources = {**prepared.evidence.evidence_sources(), **prepared.materials.work_sources}
    assert {row["source_id"] for row in payload["work_evidence_catalog"]} == {
        key for key, text in sources.items() if text.strip()
    }
    assert all(row["text"] in sources[row["source_id"]]
               for row in payload["work_evidence_catalog"])


@pytest.mark.asyncio
async def test_internal_output_selects_evidence_before_reason_and_score(tmp_path):
    run, client = workflow(StoredGateway("inquiry_reply"), tmp_path)
    await run.run(JOB)
    schema = client.responses.parse.call_args.kwargs["text_format"].model_json_schema()
    variants = [row for row in schema["$defs"].values() if "reason" in row.get("properties", {})]
    assert variants
    for variant in variants:
        fields = list(variant["properties"])
        assert fields.index("required_work_evidence_id") < fields.index("reason")
        assert fields.index("reason") < fields.index("observation") < fields.index("score")
        assert "「」" in variant["properties"]["reason"]["description"]
        assert "프로그램이 붙이므로" in variant["properties"]["reason"]["description"]
        assert set(fields) == {
            "criterion_id", "observation", "score", "reason", "required_work_evidence_id",
            "work_evidence_ids", "required_rule_evidence_id", "rule_evidence_ids",
            "review_required",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "「실제 업무 원문에는 없는 조건 문장」을 근거로 평가했습니다.",
    "[근거 없음] 선택한 업무 자료가 없다고 잘못 판단했습니다.",
])
async def test_reason_cannot_fabricate_or_deny_selected_work_evidence(tmp_path, reason):
    gateway, client = StoredGateway("inquiry_reply"), client_fixture()
    original = client.responses.parse.side_effect

    async def reply(**kwargs):
        response = await original(**kwargs)
        raw = response.output_parsed.model_dump(mode="json")
        raw["items"][4]["reason"] = reason
        response.output_parsed = kwargs["text_format"].model_validate(raw)
        return response

    client.responses.parse.side_effect = reply
    run, _ = workflow(gateway, tmp_path, client)
    with pytest.raises(EvaluationError, match="업무"):
        await run.run(JOB)
    assert client.responses.parse.await_count == 2
    assert not gateway.saved


@pytest.mark.asyncio
@pytest.mark.parametrize("index,state,condition", [
    (0, "needs_review", "needs_review"),
    (1, "needs_review", "needs_review"),
    (2, "needs_review", "needs_review"),
    (3, "unobserved", "unobserved"),
    (4, "unobserved", "unobserved"),
    (4, "needs_review", "needs_review"),
])
async def test_rubric_condition_copied_as_case_fact_is_retried_before_save(
    tmp_path, index, state, condition,
):
    gateway, client = StoredGateway("inquiry_reply"), client_fixture()
    original = client.responses.parse.side_effect
    attempts = 0

    async def reply(**kwargs):
        nonlocal attempts
        attempts += 1
        response = await original(**kwargs)
        if attempts == 1:
            # 실제 UI에서 관측한 오류: 근거 조건 문장을 해당 업무의 사실처럼 사용.
            raw = response.output_parsed.model_dump(mode="json")
            raw["items"][index].update(
                observation=state, score=None, review_required=state == "needs_review",
                reason=(raw["items"][index]["reason"].split("」", 1)[0] + "」 "
                        + gateway.rubric["items"][index][condition]),
            )
            response.output_parsed = kwargs["text_format"].model_validate(raw)
        return response

    client.responses.parse.side_effect = reply
    run, _ = workflow(gateway, tmp_path, client)
    await run.run(JOB)
    assert attempts == 2
    assert "조건 문장" in client.responses.parse.call_args.kwargs["input"][-1]["content"]
    draft = next(a for _, tool, a in gateway.calls if tool == "save_evaluation_draft")
    assert draft["items"][index]["reason"] != gateway.rubric["items"][index][condition]


@pytest.mark.asyncio
async def test_all_copied_conditions_are_reported_in_one_retry(tmp_path):
    gateway, client = StoredGateway("inquiry_reply"), client_fixture()
    original = client.responses.parse.side_effect
    attempts = 0
    copied_ids = []

    async def reply(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            feedback = kwargs["input"][-1]["content"]
            for criterion_id in copied_ids:
                assert criterion_id in feedback
        response = await original(**kwargs)
        if attempts == 1:
            raw = response.output_parsed.model_dump(mode="json")
            for index, item in enumerate(raw["items"]):
                copied_ids.append(item["criterion_id"])
                item.update(
                    observation="unobserved", score=None, review_required=False,
                    reason=(item["reason"].split("」", 1)[0] + "」 "
                            + gateway.rubric["items"][index]["unobserved"]),
                )
            response.output_parsed = kwargs["text_format"].model_validate(raw)
        return response

    client.responses.parse.side_effect = reply
    run, _ = workflow(gateway, tmp_path, client)
    await run.run(JOB)
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("index,state", [(0, "needs_review"), (3, "unobserved"), (4, "unobserved")])
async def test_unscored_result_cannot_ignore_available_case_evidence(tmp_path, index, state):
    gateway, client = StoredGateway("inquiry_reply"), client_fixture()
    original = client.responses.parse.side_effect

    async def reply(**kwargs):
        response = await original(**kwargs)
        raw = response.output_parsed.model_dump(mode="json")
        raw["items"][index].update(
            observation=state, score=None, review_required=state == "needs_review",
            required_work_evidence_id=None, work_evidence_ids=[],
            reason="[근거 없음] 테스트에서 근거 없이 내린 미관측 또는 상충 판단입니다.",
        )
        response.output_parsed = kwargs["text_format"].model_validate(raw)
        return response

    client.responses.parse.side_effect = reply
    run, _ = workflow(gateway, tmp_path, client)
    with pytest.raises(EvaluationError, match="근거"):
        await run.run(JOB)
    assert client.responses.parse.await_count == 2
    assert not gateway.saved


@pytest.mark.asyncio
async def test_saved_artifact_does_not_force_score_when_reference_is_missing(tmp_path):
    gateway, client = StoredGateway("inquiry_reply"), client_fixture()
    original = client.responses.parse.side_effect

    async def reply(**kwargs):
        response = await original(**kwargs)
        raw = response.output_parsed.model_dump(mode="json")
        raw["items"][4].update(
            observation="insufficient_data", score=None, review_required=True,
            reason=(raw["items"][4]["reason"].split("」", 1)[0] + "」 "
                    "저장된 회신 본문은 확인했으나 제품 기능을 대조할 현행 설명서가 없어 "
                    "일치 여부를 확인할 수 없습니다."),
        )
        response.output_parsed = kwargs["text_format"].model_validate(raw)
        return response

    client.responses.parse.side_effect = reply
    run, _ = workflow(gateway, tmp_path, client)
    await run.run(JOB)
    draft = next(a for _, tool, a in gateway.calls if tool == "save_evaluation_draft")
    assert draft["items"][4]["status"] == "검토 필요"
    assert draft["items"][4]["score"] is None
    assert draft["items"][4]["evidence_ids"]
