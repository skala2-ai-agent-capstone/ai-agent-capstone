import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_agents.classification.agent import classify_work
from ai_agents.cli.demo import DemoClassifier, DemoFixture, DemoPartner
from ai_agents.orchestration.approval import ReviewWorkflow, WorkflowError, validate_report
from ai_agents.shared.workflow_models import EvaluationInput, EvaluationOutput, content_hash

FIXTURE = Path(__file__).resolve().parents[1] / "samples/workflow/rfp_demo.json"


async def setup_workflow():
    fixture = DemoFixture.model_validate_json(FIXTURE.read_text())
    classification = await classify_work(
        fixture.work_record, DemoClassifier(fixture.classification)
    )
    partner = DemoPartner(fixture)
    flow = ReviewWorkflow(fixture.work_record, classification, partner)
    return flow, fixture, partner, classification


@pytest.mark.asyncio
async def test_approval_required_and_complete_report_contains_approved_content():
    flow, _, _, _ = await setup_workflow()
    draft = await flow.prepare()
    with pytest.raises(WorkflowError, match="승인한 뒤"):
        await flow.evaluate()
    flow.approve(displayed_hash=content_hash(draft))
    result = await flow.evaluate()
    assert flow.state == "completed"
    assert result.evaluation_input.approved_criteria == draft
    assert result.evaluation_input.approval.approved_at.utcoffset() is not None
    assert result.report.coaching
    with pytest.raises(WorkflowError):
        await flow.evaluate()


@pytest.mark.asyncio
async def test_revision_invalidates_approval_and_stale_display_hash():
    flow, _, _, _ = await setup_workflow()
    first = await flow.prepare()
    flow.approve(displayed_hash=content_hash(first))
    second = await flow.prepare(feedback="누락 점검 요청도 포함")
    assert flow.revision == 2
    assert content_hash(first) != content_hash(second)
    with pytest.raises(WorkflowError):
        await flow.evaluate()
    with pytest.raises(WorkflowError, match="초안이 변경"):
        flow.approve(displayed_hash=content_hash(first))
    flow.approve(displayed_hash=content_hash(second))
    assert (await flow.evaluate()).evaluation_input.approval.revision == 2


@pytest.mark.asyncio
async def test_failed_revision_cannot_restore_previous_approval():
    flow, _, partner, _ = await setup_workflow()
    draft = await flow.prepare()
    flow.approve(displayed_hash=content_hash(draft))
    partner.calls = 100
    with pytest.raises(WorkflowError):
        await flow.prepare(feedback="다시 생성")
    with pytest.raises(WorkflowError):
        await flow.evaluate()
    with pytest.raises(WorkflowError):
        flow.approve(displayed_hash=content_hash(draft))


@pytest.mark.asyncio
async def test_returned_draft_mutation_does_not_change_internal_state():
    flow, _, _, _ = await setup_workflow()
    displayed = await flow.prepare()
    original_hash = content_hash(displayed)
    displayed.items[0].title = "외부 변경"
    assert content_hash(flow.draft) == original_hash
    flow.approve(displayed_hash=original_hash)
    await flow.evaluate()


@pytest.mark.asyncio
async def test_cancel_blocks_all_progress():
    flow, _, _, _ = await setup_workflow()
    draft = await flow.prepare()
    flow.cancel()
    with pytest.raises(WorkflowError):
        flow.approve(displayed_hash=content_hash(draft))
    with pytest.raises(WorkflowError):
        await flow.prepare()
    with pytest.raises(WorkflowError):
        await flow.evaluate()


@pytest.mark.asyncio
async def test_classification_must_match_original_input():
    _, fixture, partner, report = await setup_workflow()
    fixture.work_record.messages[0].content = "다른 대화"
    with pytest.raises(WorkflowError, match="평가 대상이 다릅니다"):
        ReviewWorkflow(fixture.work_record, report, partner)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["needs_review", "multiple_tasks", "out_of_scope"])
async def test_non_single_work_is_not_silently_evaluated(status):
    _, fixture, partner, _ = await setup_workflow()
    result = fixture.classification.model_copy(deep=True)
    result.status = status
    result.work_type = None
    result.candidate_types = ["rfp_analysis", "proposal"] if status == "multiple_tasks" else []
    result.missing_information = ["업무 범위 확인"] if status == "needs_review" else []
    report = await classify_work(fixture.work_record, DemoClassifier(result))
    flow = ReviewWorkflow(fixture.work_record, report, partner)
    with pytest.raises(WorkflowError, match="평가를 보류"):
        await flow.prepare()
    assert partner.calls == 0


@pytest.mark.asyncio
async def test_wrong_criteria_type_is_rejected():
    flow, fixture, _, _ = await setup_workflow()
    fixture.drafts[0].work_type = "quotation"
    with pytest.raises(WorkflowError, match="업무 유형"):
        await flow.prepare()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["criteria", "work", "run"])
async def test_tampered_evaluation_input_fails_validation(field):
    flow, _, _, _ = await setup_workflow()
    draft = await flow.prepare()
    flow.approve(displayed_hash=content_hash(draft))
    result = await flow.evaluate()
    raw = result.evaluation_input.model_dump()
    if field == "criteria":
        raw["approved_criteria"]["items"][0]["title"] = "변조된 기준"
    elif field == "work":
        raw["work_record"]["messages"][0]["content"] = "변조된 기록"
    else:
        raw["run_id"] = "other-run"
    with pytest.raises(ValidationError):
        EvaluationInput.model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["score", "source", "quote", "version", "missing", "coaching"])
async def test_invalid_report_is_rejected(bad):
    flow, fixture, _, _ = await setup_workflow()
    draft = await flow.prepare()
    flow.approve(displayed_hash=content_hash(draft))
    if bad == "score":
        fixture.items[0].score = 1  # 승인된 점수는 0 또는 2
    elif bad == "source":
        fixture.items[0].work_evidence[0].source_id = "nonexistent"
    elif bad == "quote":
        fixture.items[0].work_evidence[0].quote = "원문에 없는 인용"
    elif bad == "version":
        fixture.items[0].rule_evidence[0].version = "other-version"
    elif bad == "missing":
        fixture.items = []
    elif bad == "coaching":
        fixture.coaching[0].criterion_id = "nonexistent"
    with pytest.raises((ValidationError, WorkflowError)):
        await flow.evaluate()
    assert flow.state == "failed"


@pytest.mark.asyncio
async def test_unobserved_has_no_score_and_duplicate_items_fail():
    flow, _, _, _ = await setup_workflow()
    draft = await flow.prepare()
    flow.approve(displayed_hash=content_hash(draft))
    result = await flow.evaluate()
    raw = result.report.model_dump()
    raw["items"][0]["observation"] = "unobserved"
    with pytest.raises(ValidationError):
        EvaluationOutput.model_validate(raw)
    raw["items"][0]["score"] = None
    raw["items"][0]["work_evidence"] = []
    output = EvaluationOutput.model_validate(raw)
    validate_report(output, result.evaluation_input)
    output.items.append(output.items[0])
    with pytest.raises(WorkflowError, match="누락·중복"):
        validate_report(output, result.evaluation_input)


@pytest.mark.parametrize("version", ["wrong-version", "demo-1"])
def test_draft_invalid_document_version_or_quote(version):
    raw = json.loads(FIXTURE.read_text())
    evidence = raw["drafts"][0]["items"][0]["rule_evidence"][0]
    evidence["version"] = version
    evidence["quote"] = "실제 원문에 없는 근거"
    with pytest.raises(ValidationError):
        DemoFixture.model_validate(raw)


def test_review_needed_is_not_given_a_score():
    from ai_agents.shared.workflow_models import ItemEvaluation

    raw = json.loads(FIXTURE.read_text())["items"][0]
    raw["observation"] = "needs_review"
    raw["review_required"] = True
    with pytest.raises(ValidationError):
        ItemEvaluation.model_validate(raw)
    raw["score"] = None
    ItemEvaluation.model_validate(raw)
    raw["review_required"] = False
    with pytest.raises(ValidationError):
        ItemEvaluation.model_validate(raw)
