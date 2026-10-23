from copy import deepcopy

import pytest
from langgraph.types import Command
from pydantic import ValidationError
from test_complete_workflow import Builder, UnifiedGateway, client_fixture
from test_mcp_integration import ReplayGateway, StubBuilder, approve

from ai_agents.evaluation_agent.final.handoff import HandoffEvaluator
from ai_agents.evaluation_agent.final.workflow import build_evaluation_workflow
from ai_agents.orchestration.graph import MCPHandoff, build_agent_graph
from ai_agents.shared.work_selection import selected_work_type
from ai_agents.shared.workflow_models import content_hash


@pytest.mark.parametrize(
    "stored,internal",
    [
        ("rfp_analysis", "rfp_analysis"),
        ("proposal", "proposal"),
        ("quote", "quotation"),
        ("inquiry_reply", "customer_response"),
        ("quotation", "quotation"),
        ("customer_response", "customer_response"),
    ],
)
def test_registered_type_mapping(stored, internal):
    result = selected_work_type(stored)
    assert result.source == "user" and result.status == "selected"
    assert result.work_type == internal
    assert "evidence" not in result.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "", "other", ["proposal", "quote"]])
async def test_missing_invalid_or_multiple_selection_stops_before_rag(invalid):
    gateway, builder = ReplayGateway(), StubBuilder()
    gateway.raw["work_case"]["work_type"] = invalid
    graph = build_agent_graph(gateway, None, builder)
    result = await graph.ainvoke(
        {"work_id": gateway.wid},
        {"configurable": {"thread_id": "invalid-type"}},
    )
    assert result["status"] == "work_type_hold"
    assert result["issue"] == "USER_WORK_TYPE_REQUIRED"
    assert builder.calls == 0
    assert not any(server == "rag" for server, _, _ in gateway.calls)


@pytest.mark.asyncio
async def test_selected_type_ignores_conversation_labels_and_binds_handoff(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("사용자 선택 흐름에서 분류 LLM을 호출하면 안 된다")

    monkeypatch.setattr("ai_agents.orchestration.graph.classify_work", forbidden)
    gateway = ReplayGateway()  # RFP 대화라도 사용자가 저장한 proposal을 사용한다.
    gateway.raw["work_case"]["work_type"] = "proposal"
    graph = build_agent_graph(gateway, None, StubBuilder())
    assert "select_work_type" in graph.get_graph().nodes
    assert "classify_work" not in graph.get_graph().nodes
    config = {"configurable": {"thread_id": "selected-type"}}
    pending = await graph.ainvoke({"work_id": gateway.wid}, config)
    assert pending["criteria"]["work_type"] == "proposal"
    selection = pending["__interrupt__"][0].value["classification"]
    assert selection["status"] == "selected" and selection["source"] == "user"
    result = await graph.ainvoke(Command(resume=approve(pending)), config)
    handoff = MCPHandoff.model_validate(result["handoff"])
    assert handoff.evaluation_input.classification.work_type == "proposal"

    # 기존 packet hash도 다시 계산한 공격: 선택과 저장 유형의 별도 일치 검사로 막는다.
    bad = deepcopy(result["handoff"])
    bad["business"]["work_case"]["work_type"] = "rfp_analysis"
    packet = handoff.business.model_copy(deep=True)
    packet.work_case["work_type"] = "rfp_analysis"
    bad["approved_business_sha256"] = content_hash(packet)
    with pytest.raises(ValidationError, match="사용자가 선택한 업무 유형"):
        MCPHandoff.model_validate(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_selected_type_reaches_existing_evaluator(work_type, monkeypatch):
    gateway, client = UnifiedGateway(work_type), client_fixture()
    original_call = gateway.call

    async def call(server, tool, arguments):
        result = await original_call(server, tool, arguments)
        if tool == "get_work_case":
            result["data"]["work_type"] = work_type
        return result

    monkeypatch.setattr(gateway, "call", call)
    workflow = build_evaluation_workflow(
        gateway,
        None,
        Builder(),
        evaluator=HandoffEvaluator(client, gateway),
    )
    config = {"configurable": {"thread_id": f"manual-complete-{work_type}"}}
    pending = await workflow.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    assert pending["status"] == "awaiting_approval"
    client.responses.parse.assert_not_awaited()
    result = await workflow.ainvoke(Command(resume=approve(pending)), config)
    assert result["status"] == "completed"
    assert result["handoff"]["evaluation_input"]["classification"]["source"] == "user"
    assert len(result["report"]["items"]) == 5
