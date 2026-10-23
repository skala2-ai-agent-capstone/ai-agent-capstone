"""RFP 원문에 있는 금액 언급과 견적 계산 평가를 구분한다."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command
from pydantic import ValidationError
from test_mcp_integration import ReplayGateway, StubClassifier
from test_rubric_builder import fake_policy, fake_rubric, response

from ai_agents.cli.mcp import human_decision
from ai_agents.criteria_agent.builder import (
    GUIDANCE,
    OpenAIRubricBuilder,
    RubricError,
    WorkTypeRubric,
    adapt_rubric,
    rubric_issues,
)
from ai_agents.orchestration.graph import build_agent_graph
from ai_agents.shared.workflow_models import CriteriaDraft, content_hash


@pytest.mark.parametrize("index", [2, 4])
@pytest.mark.parametrize(
    "field,text",
    [
        (
            "score_0",
            "중요한 오류가 있거나 다수의 요구사항이 누락되었고, "
            "금액·기능 등 중요한 오류가 포함되어 있다.",
        ),
        ("criterion", "고객이 제시한 예산 금액 조건을 RFP 원문과 대응표에서 대조했는가."),
        ("score_2", "가격표·단가에 관한 고객 요구사항을 원문 위치와 함께 빠짐없이 추출했다."),
        ("criterion", "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다"),
        ("score_2", "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다"),
    ],
)
def test_rfp_financial_terms_alone_do_not_block_approval(index, field, text):
    rubric = fake_rubric("rfp_analysis")
    setattr(rubric.rows[index], field, text)
    draft = adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))
    assert len(draft.items) == 5
    assert any(
        w.criterion_id == draft.items[index].id and w.excerpt == text for w in draft.warnings
    )


@pytest.mark.parametrize("index", [2, 4])
@pytest.mark.parametrize(
    "text",
    [
        "단가와 수량을 곱해 총액을 계산했는가.",
        "할인율을 적용해 최종 견적을 작성했는가.",
        "금액 재계산을 수행하고 견적 계산 오류를 수정했는가.",
        "견적 금액을 산출했는지 평가한다.",
    ],
)
def test_rfp_still_rejects_quotation_calculation_criteria(index, text):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[index].criterion = text
    with pytest.raises(RubricError, match="다른 업무의 내용"):
        adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))


@pytest.mark.parametrize(
    "text",
    [
        "금액 계산은 평가 대상에서 제외한다",
        "금액 계산은 평가하지 않는다.",
        "금액 계산 여부는 평가 대상이 아니다.",
        "견적 금액을 재계산했는지는 평가하지 않는다.",
        "RFP 원문에서 금액 계산을 요구한 내용을 정확히 추출했는지 평가한다.",
    ],
)
def test_exclusions_and_requirement_mentions_are_review_warnings(text):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = text
    policy = fake_policy("rfp_analysis")
    assert rubric_issues(rubric, "rfp_analysis", policy) == []
    draft = adapt_rubric(rubric, "rfp_analysis", policy)
    assert draft.warnings[0].excerpt == text
    assert draft.warnings[0].code == "scope_review"


@pytest.mark.parametrize(
    "text",
    [
        "견적 금액을 재계산했는지 평가한다",
        "금액 계산은 제외한다. 견적 금액을 재계산했는지 평가한다.",
        "금액 계산은 제외하되 할인율을 적용해 최종 견적을 작성했는지 평가한다.",
        "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다. "
        "견적 금액을 산출했는지 평가한다.",
        "예산 금액 조건이 산출물에 정확히 반영됐는지와 "
        "견적 금액을 산출했는지 평가한다.",
    ],
)
def test_clear_foreign_assessment_requests_revision_even_after_exclusion(text):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = text
    with pytest.raises(RubricError, match="해당 평가 조건을 수정"):
        adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_id",
        "required_reference",
        "missing_item",
        "wrong_score_type",
        "missing_score",
        "security_middle_score",
        "duplicate_scores",
    ],
)
def test_warning_does_not_bypass_strict_validation(invalid):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = "금액 계산은 평가 대상에서 제외한다"
    if invalid == "unknown_id":
        rubric.rows[4].related_clauses.append("ART-999")
    elif invalid == "required_reference":
        rubric.rows[4].related_clauses = ["APX-5"]
    elif invalid == "missing_item":
        rubric.rows.pop()
    elif invalid == "wrong_score_type":
        raw = rubric.model_dump()
        raw["rows"][4]["score_2"] = 2
        with pytest.raises(ValidationError):
            WorkTypeRubric.model_validate(raw)
        return
    elif invalid == "missing_score":
        rubric.rows[4].score_2 = ""
    elif invalid == "security_middle_score":
        rubric.rows[3].score_1 = "일부 준수"
    else:
        rubric.rows[4].score_2 = rubric.rows[4].score_0
    with pytest.raises((RubricError, ValidationError)):
        adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "금액 계산은 평가 대상에서 제외한다",
        "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다",
    ],
)
async def test_warning_does_not_trigger_another_generation_request(text):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = text
    parse = AsyncMock(return_value=response(rubric))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    draft = await builder.generate("rfp_analysis", fake_policy("rfp_analysis"))
    assert draft.warnings
    assert parse.await_count == 1


@pytest.mark.asyncio
async def test_foreign_assessment_is_sent_as_correction_request():
    invalid = fake_rubric("rfp_analysis")
    invalid.rows[4].criterion = "견적 금액을 재계산했는지 평가한다"
    valid = fake_rubric("rfp_analysis")
    parse = AsyncMock(side_effect=[response(invalid), response(valid)])
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    draft = await builder.generate("rfp_analysis", fake_policy("rfp_analysis"))
    assert not draft.warnings
    assert parse.await_count == 2
    assert "해당 평가 조건을 수정" in parse.call_args.kwargs["input"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", list(GUIDANCE["work_types"]))
async def test_generation_payload_contains_only_the_selected_task_scope(work_type):
    parse = AsyncMock(return_value=response(fake_rubric(work_type)))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    await builder.generate(work_type, fake_policy(work_type))
    payload = json.loads(parse.call_args.kwargs["input"][1]["content"])
    scope = payload["task"]["generation_scope"]
    assert scope == GUIDANCE["work_types"][work_type]["generation_scope"]
    assert scope["evaluate"] and scope["exclude"] and scope["allowed_mentions"]
    assert "scope_review_terms" in payload["item_instructions"][0]
    assert "task.generation_scope" in parse.call_args.kwargs["input"][0]["content"]
    if work_type == "rfp_analysis":
        assert "필수/선택" in " ".join(scope["evaluate"])
        assert "대응 가능" in " ".join(scope["evaluate"])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,status", [("승인", "ready_for_evaluation"), ("취소", "cancelled")])
@pytest.mark.parametrize(
    "text",
    [
        "금액 계산은 평가 대상에서 제외한다",
        "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다",
    ],
)
async def test_warnings_reach_approval_screen(monkeypatch, capsys, answer, status, text):
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = text
    parse = AsyncMock(return_value=response(rubric))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    gateway = ReplayGateway()
    graph = build_agent_graph(gateway, StubClassifier(), builder)
    config = {"configurable": {"thread_id": f"scope-warning-{answer}"}}
    pending = await graph.ainvoke({"work_id": gateway.wid}, config)
    prompt = pending["__interrupt__"][0].value
    assert pending["status"] == "awaiting_approval"
    assert "handoff" not in pending
    assert prompt["warnings"] == prompt["criteria"]["warnings"]
    assert prompt["warnings"][0]["excerpt"] == rubric.rows[4].criterion
    monkeypatch.setattr("builtins.input", lambda: answer)
    decision = human_decision(prompt)
    shown = capsys.readouterr().err
    assert "검토 경고" in shown and rubric.rows[4].criterion in shown
    result = await graph.ainvoke(Command(resume=decision), config)
    assert result["status"] == status
    assert parse.await_count == 1
    if answer == "승인":
        approved = result["handoff"]["evaluation_input"]["approved_criteria"]
        assert approved["warnings"] == prompt["warnings"]
        assert content_hash(CriteriaDraft.model_validate(approved)) == prompt["criteria_sha256"]


@pytest.mark.asyncio
async def test_revision_replaces_warnings_and_invalidates_old_approval():
    first = fake_rubric("rfp_analysis")
    first.rows[4].criterion = "금액 계산은 평가 대상에서 제외한다"
    parse = AsyncMock(side_effect=[response(first), response(fake_rubric("rfp_analysis"))])
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    gateway = ReplayGateway()
    graph = build_agent_graph(gateway, StubClassifier(), builder)
    config = {"configurable": {"thread_id": "scope-warning-revision"}}
    pending = await graph.ainvoke({"work_id": gateway.wid}, config)
    old = pending["__interrupt__"][0].value
    old_approval = {key: old[key] for key in ("criteria_sha256", "business_sha256", "revision")}
    revised = await graph.ainvoke(
        Command(
            resume={
                **old_approval,
                "action": "revise",
                "feedback": "요구사항 대응 품질에 집중해 다시 작성",
            }
        ),
        config,
    )
    current = revised["__interrupt__"][0].value
    assert current["revision"] == 2
    assert current["warnings"] == current["criteria"]["warnings"] == []
    stale = await graph.ainvoke(Command(resume={**old_approval, "action": "approve"}), config)
    assert stale["status"] == "awaiting_approval" and "handoff" not in stale
    assert parse.await_count == 2


@pytest.mark.parametrize(
    "work_type,text",
    [
        ("rfp_analysis", "금액"),
        ("proposal", "금액 산출"),
        ("quotation", "승인 결과"),
        ("customer_response", "금액 산출"),
    ],
)
def test_term_detection_alone_is_warning_for_every_work_type(work_type, text):
    rubric = fake_rubric(work_type)
    rubric.rows[2].criterion = text
    draft = adapt_rubric(rubric, work_type, fake_policy(work_type))
    assert draft.warnings
