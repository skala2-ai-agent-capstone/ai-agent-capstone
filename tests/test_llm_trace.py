"""기록 프록시용 헤더: 실제 SDK + 가짜 HTTP 전송과 평가 그래프 대역으로 확인. 외부 API 호출 없음."""

import asyncio
import json

import httpx
import pytest
from langgraph.types import Command
from openai import AsyncOpenAI
from test_complete_workflow import Builder, Classifier, UnifiedGateway, client_fixture
from test_mcp_integration import approve
from test_provider import DECISION

from ai_agents.classification.agent import classify_work
from ai_agents.classification.provider import OpenAIClassifier
from ai_agents.evaluation_agent.final.handoff import HandoffEvaluator
from ai_agents.evaluation_agent.final.workflow import build_evaluation_workflow
from ai_agents.shared.llm_trace import trace_headers, work_scope
from ai_agents.shared.models import Message, WorkContext


def test_trace_headers_follow_work_scope():
    assert trace_headers("classify") == {"X-Stage": "classify"}
    with work_scope("TASK-1"):
        with work_scope("TASK-2"):
            assert trace_headers("rubric") == {"X-Stage": "rubric", "X-Work-Id": "TASK-2"}
        assert trace_headers("rubric")["X-Work-Id"] == "TASK-1"
    assert trace_headers("evaluate") == {"X-Stage": "evaluate"}


def test_work_scope_rejects_id_the_proxy_would_truncate():
    with pytest.raises(ValueError, match="업무 ID 길이"):
        with work_scope("W" * 129):
            pass
    assert trace_headers("rubric") == {"X-Stage": "rubric"}


@pytest.mark.parametrize("scope", [None, "TASK-2291"])
def test_classification_sends_work_id_only_as_header(scope):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1,
            "status": "completed", "model": "test-model",
            "output": [{
                "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "annotations": [],
                             "text": json.dumps(DECISION, ensure_ascii=False)}],
            }],
        })

    context = WorkContext(
        work_id="TASK-2291",
        messages=[Message(id="m1", role="user", content="금액을 계산해줘.")],
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with AsyncOpenAI(
                api_key="fake-test-key", http_client=http_client, max_retries=0
            ) as client:
                with work_scope(scope):
                    return await classify_work(context, OpenAIClassifier(client, "test-model"))

    report = asyncio.run(run())
    assert report.classification.work_type == "quotation"
    [request] = seen
    assert request.headers["x-stage"] == "classify"
    # 업무 범위 밖(샘플·가상 업무)에서는 프록시가 거절하지 않도록 X-Work-Id를 보내지 않는다
    assert request.headers.get("x-work-id") == scope
    # 분류 힌트가 되지 않도록 업무 ID는 본문(프롬프트)에 없다
    assert "TASK-2291" not in request.content.decode()
    # 분류가 끝나면 업무 범위가 남지 않는다
    assert trace_headers("classify") == {"X-Stage": "classify"}


@pytest.mark.asyncio
async def test_graph_records_mcp_work_id_for_every_llm_stage():
    gateway, client = UnifiedGateway("proposal"), client_fixture()
    work_id = gateway.fixture.context.work_id
    seen = {}

    class TracedClassifier(Classifier):
        async def classify(self, **kwargs):
            seen["classify"] = trace_headers("classify")
            return await super().classify(**kwargs)

    class TracedBuilder(Builder):
        async def generate(self, *args, **kwargs):
            seen["rubric"] = trace_headers("rubric")
            return await super().generate(*args, **kwargs)

    graph = build_evaluation_workflow(
        gateway, TracedClassifier("proposal"), TracedBuilder(),
        evaluator=HandoffEvaluator(client, gateway),
    )
    config = {"configurable": {"thread_id": "llm-trace"}}
    pending = await graph.ainvoke({"work_id": work_id}, config)
    result = await graph.ainvoke(Command(resume=approve(pending)), config)

    assert result["status"] == "completed"
    assert seen == {
        "classify": {"X-Stage": "classify", "X-Work-Id": work_id},
        "rubric": {"X-Stage": "rubric", "X-Work-Id": work_id},
    }
    assert client.responses.parse.await_args.kwargs["extra_headers"] == {
        "X-Stage": "evaluate", "X-Work-Id": work_id,
    }
