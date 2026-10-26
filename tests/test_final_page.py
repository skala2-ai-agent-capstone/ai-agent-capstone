import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_stored_final_evaluation import JOB, StoredGateway, workflow

from ai_agents.cli.final_report import main
from ai_agents.evaluation_agent.final.page import build_final_page, build_report_save_request
from ai_agents.evaluation_agent.final.validation import EvaluationError
from ai_agents.orchestration.final_evaluation import PreparedFinalEvaluation
from ai_agents.orchestration.final_worker import RESULT_QUEUE, FinalEvaluationWorker
from ai_agents.shared.final_job_contracts import FinalEvaluationEvent
from ai_agents.shared.final_page_contracts import (
    ITEM_NAMES,
    FinalEvaluationPage,
    FinalReportSaveRequest,
)
from ai_agents.shared.workflow_models import content_hash


async def prepared_result(tmp_path, work_type="proposal"):
    gateway = StoredGateway(work_type)
    run, client = workflow(gateway, tmp_path)
    result = await run.run(JOB)
    prepared = PreparedFinalEvaluation.model_validate_json(Path(result["checkpoint"]).read_text())
    return prepared, result, gateway, client


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_page_uses_original_ids_versions_and_verified_evidence(tmp_path, work_type):
    prepared, result, gateway, client = await prepared_result(tmp_path, work_type)
    page = build_final_page(prepared)
    assert page == prepared.page_result
    assert [row.title for row in page.items] == list(ITEM_NAMES)
    assert {row.item_id for row in page.items} == set(
        prepared.request.stored_rubric.item_ids().values()
    )
    assert page.work.work_type == work_type
    assert page.work.artifact_version == prepared.evidence.context.target.version
    assert page.rubric.version == prepared.request.stored_rubric.version
    assert page.score.display == "0/10" and page.score.scored_count == 5
    for row in page.items:
        assert row.work_evidence and row.rule_evidence and row.coaching
        assert row.criterion == next(
            i.criterion for i in prepared.request.stored_rubric.items if i.item_id == row.item_id
        )
    assert [level.score for level in page.items[3].score_levels] == [0, 2]
    assert result["page_result"] == {
        "schema_version": "1", "sha256": content_hash(page), "persistence": "checkpoint_only",
    }
    assert "items" not in result["page_result"]
    assert client.responses.parse.await_count == 1
    assert [t for _, t, _ in gateway.calls if t.startswith("save_")] == [
        "save_evaluation_draft", "save_coaching",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("observation,label", [
    ("unobserved", "미관측"), ("insufficient_data", "자료 부족"), ("needs_review", "검토 필요"),
])
async def test_excluded_items_do_not_change_score_denominator(tmp_path, observation, label):
    prepared, *_ = await prepared_result(tmp_path)
    item = prepared.report.items[0]
    item.observation, item.score = observation, None
    item.review_required = observation != "unobserved"
    page = build_final_page(prepared)
    assert page.items[0].status_label == label and page.items[0].score is None
    assert not page.items[0].included_in_total
    assert page.score.display == "0/8" and page.score.coverage == "partial"
    assert page.score.excluded_count == 1
    assert page.review_required is (observation != "unobserved")


@pytest.mark.asyncio
async def test_no_scored_items_has_no_numeric_total(tmp_path):
    prepared, *_ = await prepared_result(tmp_path)
    for item in prepared.report.items:
        item.observation, item.score = "unobserved", None
    page = build_final_page(prepared)
    assert page.score.earned is None and page.score.possible == 0
    assert page.score.display == "산정 불가" and page.score.coverage == "none"
    assert "0/0" not in page.summary and "산정 불가" in page.summary


@pytest.mark.asyncio
async def test_partial_positive_scores_and_shuffled_items_keep_display_order(tmp_path):
    prepared, *_ = await prepared_result(tmp_path)
    scores = {"problem_definition": 2, "instruction_quality": 2, "verification": 1, "quality": 2}
    for item in prepared.report.items:
        key = item.criterion_id.rsplit(".", 1)[1]
        if key == "security":
            item.observation, item.score = "unobserved", None
        else:
            item.score = scores[key]
    prepared.report.items.reverse()
    page = build_final_page(prepared)
    assert [item.title for item in page.items] == list(ITEM_NAMES)
    assert page.score.earned == 7 and page.score.possible == 8
    assert page.score.display == "7/8" and "7/8점" in page.summary


@pytest.mark.asyncio
async def test_missing_data_always_shows_review_in_summary(tmp_path):
    prepared, *_ = await prepared_result(tmp_path)
    item = prepared.report.items[0]
    item.observation, item.score = "insufficient_data", None
    item.reason = "업무 기록은 있으나 대조에 필요한 자료가 수집되지 않았습니다."
    item.review_required = False  # 이전 평가 결과가 이 플래그를 생략한 경우도 상태로 판단한다.
    page = build_final_page(prepared)
    assert page.review_required and "검토 필요 1개" in page.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["quote", "run", "criteria", "coaching"])
async def test_page_revalidates_sources_before_export(tmp_path, tamper):
    prepared, result, *_ = await prepared_result(tmp_path)
    if tamper == "quote":
        prepared.report.items[0].work_evidence[0].quote = "fabricated-sensitive-text"
    elif tamper == "run":
        prepared.report.run_id = "ANOTHER-RUN"
    elif tamper == "criteria":
        prepared.report.criteria_sha256 = "f" * 64
    else:
        prepared.report.coaching.pop()
    with pytest.raises((EvaluationError, ValidationError)):
        build_final_page(prepared)
    Path(result["checkpoint"]).write_text(prepared.model_dump_json())
    output = tmp_path / "must-not-exist.json"
    assert main(["export", "--checkpoint", result["checkpoint"], "--output", str(output)]) == 2
    assert not output.exists()


@pytest.mark.asyncio
async def test_save_proposal_binds_two_revisions_and_does_not_trust_cached_page(tmp_path):
    prepared, *_ = await prepared_result(tmp_path)
    request = build_report_save_request(prepared)
    assert request.evaluation_id == JOB.evaluation_id
    assert request.draft_revision == request.coaching_revision == 1
    assert request.report_sha256 == content_hash(request.report)
    assert request.idempotency_key == f"final:{JOB.key()}:report:v1"
    prepared.page_result.summary = "untrusted cached summary"
    assert build_report_save_request(prepared) == request
    modified = request.model_dump(mode="json")
    modified["evaluation_id"] = "OTHER"
    with pytest.raises(ValidationError):
        FinalReportSaveRequest.model_validate(modified)
    modified = request.model_dump(mode="json")
    modified["report_sha256"] = "a" * 64
    with pytest.raises(ValidationError):
        FinalReportSaveRequest.model_validate(modified)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "other-evaluation", "boolean-revision"])
async def test_export_requires_valid_save_receipts(tmp_path, change):
    prepared, *_ = await prepared_result(tmp_path)
    receipt = prepared.receipts["save_coaching"]
    if change == "missing":
        prepared.receipts.pop("save_coaching")
    elif change == "other-evaluation":
        receipt["evaluation_id"] = "OTHER"
    else:
        receipt["coaching_revision"] = True
    with pytest.raises(EvaluationError):
        build_report_save_request(prepared)


@pytest.mark.asyncio
async def test_old_checkpoint_gets_page_without_model_or_save_calls(tmp_path):
    _, result, gateway, _ = await prepared_result(tmp_path)
    checkpoint = Path(result["checkpoint"])
    original = json.loads(checkpoint.read_text())
    original.pop("page_result")
    checkpoint.write_text(json.dumps(original))
    gateway.calls.clear()
    run, client = workflow(gateway, tmp_path)
    resumed = await run.run(JOB)
    assert resumed["page_result"]["persistence"] == "checkpoint_only"
    assert json.loads(checkpoint.read_text())["page_result"]["evaluation_id"] == JOB.evaluation_id
    client.responses.parse.assert_not_awaited()
    assert [t for _, t, _ in gateway.calls] == ["get_evaluation_context"]


@pytest.mark.asyncio
async def test_cli_exports_private_file_without_printing_report_or_overwriting(tmp_path, capsys):
    prepared, result, *_ = await prepared_result(tmp_path)
    path = tmp_path / "page.json"
    args = ["export", "--checkpoint", result["checkpoint"], "--output", str(path)]
    assert main(args) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert FinalEvaluationPage.model_validate_json(path.read_text()) == prepared.page_result
    stdout = capsys.readouterr().out
    assert prepared.page_result.items[0].reason not in stdout
    assert json.loads(stdout)["status"] == "exported"
    original = path.read_bytes()
    assert main(args) == 2 and path.read_bytes() == original
    proposal = tmp_path / "save-request.json"
    assert main([
        "export", "--checkpoint", result["checkpoint"], "--output", str(proposal), "--save-request",
    ]) == 0
    FinalReportSaveRequest.model_validate_json(proposal.read_text())


@pytest.mark.asyncio
async def test_worker_result_message_does_not_expose_personal_page(tmp_path):
    prepared, result, *_ = await prepared_result(tmp_path)
    publisher = AsyncMock()
    worker = FinalEvaluationWorker(
        AsyncMock(return_value=result), trace_dir=tmp_path / "traces", model="test",
        code_provenance={},
    )
    event = FinalEvaluationEvent(message_id="test", **JOB.model_dump())
    await worker.handle(event.model_dump_json().encode(), publish=publisher, ack=AsyncMock())
    queue, payload, _ = publisher.await_args.args
    assert queue == RESULT_QUEUE and "page_result" not in payload
    assert prepared.page_result.items[0].reason not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_page_schema_rejects_derived_score_tampering(tmp_path):
    prepared, *_ = await prepared_result(tmp_path)
    original = prepared.page_result.model_dump(mode="json")
    for change in ("score", "security", "duplicate", "confirmed"):
        value = deepcopy(original)
        if change == "score":
            value["score"]["earned"] = 10
        elif change == "security":
            value["items"][3]["score"] = 1
        elif change == "duplicate":
            value["items"][1]["item_id"] = value["items"][0]["item_id"]
        else:
            value["can_confirm"] = True
        with pytest.raises(ValidationError):
            FinalEvaluationPage.model_validate(value)
