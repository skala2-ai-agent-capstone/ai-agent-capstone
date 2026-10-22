import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langsmith import Client
from langsmith.run_helpers import get_tracing_context

from ai_agents.observability import TraceSession, trace_step
from ai_agents.observability.tracing import LangSmithSink, TracedLLMClient


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_nested_async_traces_do_not_capture_raw_or_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    secret = "Bearer private-key kim@example.com 010-1234-5678"

    @trace_step("child", run_type="tool")
    async def child(value):
        assert get_tracing_context()["enabled"] is False
        return {"raw": value}

    with TraceSession(tmp_path, metadata={"purpose": "feedback", "token": secret}) as session:
        with session.span("root", inputs={"token": secret}):
            assert await child(secret) == {"raw": secret}
            with pytest.raises(ValueError):
                with session.span("error"):
                    raise ValueError(secret)
    raw = session.path.read_text()
    assert secret not in raw and "example.com" not in raw and "private-key" not in raw
    records = events(session.path)
    root = records[0]
    assert root["span_id"] == session.trace_id
    child_event = next(e for e in records if e["name"] == "child" and e["event"] == "end")
    assert child_event["parent_id"] == root["span_id"]
    assert child_event["duration_ms"] >= 0
    assert (
        next(e for e in records if e["name"] == "error" and e["event"] == "end")["error_type"]
        == "ValueError"
    )


@pytest.mark.asyncio
async def test_concurrent_children_have_correct_parent(tmp_path):
    @trace_step("child")
    async def child(i):
        await asyncio.sleep(0)
        return i

    with TraceSession(tmp_path) as session:
        with session.span("root"):
            assert await asyncio.gather(child(1), child(2)) == [1, 2]
    starts = [e for e in events(session.path) if e["name"] == "child" and e["event"] == "start"]
    assert len(starts) == 2
    assert all(e["parent_id"] == session.trace_id for e in starts)


def test_langsmith_sink_posts_only_summaries_and_flushes(tmp_path):
    client = Mock(spec=Client)
    sink = LangSmithSink("unit-test", client=client)
    with TraceSession(tmp_path, sink=sink) as session:
        with session.span("root", inputs={"content": "private text"}) as span:
            with session.span("child"):
                pass
            span.result({"reply": "private output"})
    assert client.create_run.call_count == 2
    assert client.update_run.call_count == 2
    client.flush.assert_called_once()
    assert "private text" not in str(client.mock_calls)
    assert "private output" not in str(client.mock_calls)


def test_export_failure_preserves_local_result(tmp_path):
    sink = Mock()
    sink.start.side_effect = RuntimeError("secret export failure")
    with pytest.warns(RuntimeWarning, match="Trace export failed"):
        with TraceSession(tmp_path, sink=sink) as session:
            with session.span("root") as span:
                span.result(7)
    assert session.telemetry_failed
    assert events(session.path)[-1]["error_type"] is None


def test_langsmith_requires_explicit_flag_and_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        with TraceSession(tmp_path, langsmith=True):
            pass


@pytest.mark.asyncio
async def test_llm_proxy_preserves_arguments_and_returns_usage_only(tmp_path):
    reply = SimpleNamespace(
        output_parsed={"content": "private"},
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
    )
    parse = AsyncMock(return_value=reply)
    client = TracedLLMClient(SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    with TraceSession(tmp_path) as session:
        with session.span("root"):
            assert await client.responses.parse(model="test", input="private") is reply
    parse.assert_awaited_once_with(model="test", input="private")
    llm = next(
        e
        for e in events(session.path)
        if e["name"] == "llm.responses.parse" and e["event"] == "end"
    )
    assert llm["outputs"]["usage_metadata"]["total_tokens"] == 14
    assert "private" not in session.path.read_text()


@pytest.mark.asyncio
async def test_cli_disables_ambient_auto_tracing_without_trace_directory(monkeypatch):
    from ai_agents.cli import workflow as cli

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)

    async def execute(args):
        assert get_tracing_context()["enabled"] is False
        return {"status": "completed"}

    monkeypatch.setattr(cli, "_run", execute)
    assert await cli.run(SimpleNamespace(trace_dir=None, langsmith=False)) == {
        "status": "completed"
    }


@pytest.mark.asyncio
async def test_existing_graph_approval_and_evaluation_are_traced_without_network(tmp_path):
    from langgraph.types import Command
    from test_complete_workflow import (
        Builder,
        Classifier,
        UnifiedGateway,
        approve,
        client_fixture,
    )

    from ai_agents.evaluation_agent.final.handoff import HandoffEvaluator
    from ai_agents.evaluation_agent.final.workflow import build_evaluation_workflow

    gateway, client = UnifiedGateway(), client_fixture()
    workflow = build_evaluation_workflow(
        gateway,
        Classifier("proposal"),
        Builder(),
        evaluator=HandoffEvaluator(TracedLLMClient(client), gateway),
    )
    config = {"configurable": {"thread_id": "traced-approval"}}
    with TraceSession(tmp_path) as session:
        with session.span("evaluation_request"):
            pending = await workflow.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
            assert pending["status"] == "awaiting_approval"
            client.responses.parse.assert_not_awaited()
            result = await workflow.ainvoke(Command(resume=approve(pending)), config)
    assert result["status"] == "completed"
    starts = [e for e in events(session.path) if e["event"] == "start"]
    by_name = {e["name"]: e for e in starts}
    assert {
        "evaluation_workflow",
        "evaluate_approved_work",
        "validate_evaluation_receipt",
        "llm.responses.parse",
    } <= by_name.keys()
    assert (
        by_name["llm.responses.parse"]["parent_id"] == by_name["evaluate_approved_work"]["span_id"]
    )
    assert all(e["trace_id"] == session.trace_id for e in starts)
