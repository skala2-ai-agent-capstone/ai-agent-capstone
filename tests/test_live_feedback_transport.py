"""MCP는 실제 SDK/ASGI로 검증하고, RabbitMQ는 전송 객체를 대체한다."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from ai_agents.evaluation_agent.feedback.agent import FeedbackAgent
from ai_agents.evaluation_agent.feedback.errors import FeedbackError
from ai_agents.integrations.live_mcp import LiveMCPGateway
from ai_agents.orchestration.feedback_worker import DLQ, REQUEST_QUEUE, RETRY_QUEUE
from ai_agents.orchestration.live_feedback import LiveFeedbackWorkflow
from ai_agents.shared.live_feedback_contracts import LiveEvaluationEvent
from ai_agents.validation.feedback_targets import FixtureChatClient


@pytest.mark.asyncio
async def test_sdk_http_workflow_auth_and_save(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / "samples/feedback/complete-five.json").read_text())
    server, saved, auth = MCPServer("feedback-contract-test"), [], []

    @server.tool()
    async def start_live_evaluation(live_eval_id: str) -> dict:
        assert live_eval_id == fixture["event"]["live_eval_id"]
        return {"ok": True, "data": fixture["context"]}

    @server.tool()
    async def get_messages(live_eval_id: str, work_id: str, from_seq: int, to_seq: int) -> dict:
        assert (live_eval_id, work_id, from_seq, to_seq) == (
            fixture["event"]["live_eval_id"],
            fixture["event"]["work_id"],
            1,
            10,
        )
        return {"ok": True, "data": fixture["batch"]}

    @server.tool()
    async def save_live_evaluation(
        live_eval_id: str,
        status: str,
        items: list[dict],
        coaching: dict,
        model: str,
        latency_ms: int,
        error: str | None = None,
    ) -> dict:
        saved.append({"items": items, "coaching": coaching, "status": status})
        return {
            "ok": True,
            "data": {
                "live_eval_id": live_eval_id,
                "status": status,
                "duplicate": False,
            },
        }

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            auth.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)

    monkeypatch.setattr(
        "ai_agents.integrations.live_mcp.create_mcp_http_client",
        lambda headers: httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi), headers=headers
        ),
    )
    async with app.router.lifespan_context(app):
        async with LiveMCPGateway("test-secret", "http://testserver/mcp") as gateway:
            result = await LiveFeedbackWorkflow(
                gateway,
                FeedbackAgent(FixtureChatClient(fixture["model_output"]), "fixture-model"),
            ).run(
                LiveEvaluationEvent.model_validate(fixture["event"]),
                trace_id="test-trace",
                code_provenance={},
            )
            with pytest.raises(FeedbackError, match="MCP_REJECTED"):
                await gateway.call("finalize", {})
    assert result["status"] == "완료" and len(saved) == 1
    assert saved[0]["coaching"]["provenance"]["exchange_ids"] == [101, 102, 103, 104, 105]
    assert auth and all(value == b"Bearer test-secret" for value in auth)


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable,expected", [(True, "MCP_UNAVAILABLE"), (False, "MCP_REJECTED")])
async def test_gateway_classifies_server_error(retryable, expected):
    gateway = LiveMCPGateway("test-secret", "http://testserver/mcp")
    gateway._client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=False,
                structured_content={
                    "ok": False,
                    "error": {
                        "code": "TEMPORARY_FAILURE",
                        "retryable": retryable,
                    },
                },
                content=[],
            )
        )
    )
    with pytest.raises(FeedbackError, match=expected) as raised:
        await gateway.call("get_messages", {})
    assert raised.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["ack", "nack", "missing"])
async def test_rabbit_confirms_before_ack_and_requeues_on_failure(monkeypatch, confirm):
    aio_pika = pytest.importorskip("aio_pika")
    from pamqp.commands import Basic

    from ai_agents.integrations.feedback_rabbit import consume_feedback

    calls, sent = [], []

    class Incoming:
        body = b"{}"
        processed = False

        async def ack(self):
            calls.append("ack")
            self.processed = True

        async def nack(self, *, requeue):
            assert requeue
            calls.append("nack")
            self.processed = True

    class Queue:
        def iterator(self, *, no_ack):
            assert not no_ack
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            async def messages():
                yield Incoming()

            return messages()

    async def publish(message, **kwargs):
        calls.append("confirm")
        sent.append((message, kwargs))
        return {"ack": Basic.Ack(), "nack": Basic.Nack(), "missing": None}[confirm]

    channel = SimpleNamespace(
        set_qos=AsyncMock(),
        get_queue=AsyncMock(return_value=Queue()),
        default_exchange=SimpleNamespace(publish=publish),
    )

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def channel(self, **kwargs):
            assert kwargs == {"publisher_confirms": True, "on_return_raises": True}
            return channel

    async def handle(body, *, publish, ack):
        await publish(RETRY_QUEUE, {"message_id": "m-1"}, "MCP_UNAVAILABLE")
        await ack()

    monkeypatch.setattr(aio_pika, "connect_robust", AsyncMock(return_value=Connection()))
    monkeypatch.setattr("ai_agents.integrations.feedback_rabbit.asyncio.sleep", AsyncMock())
    await consume_feedback("amqp://unused", SimpleNamespace(handle=handle))
    assert calls == ["confirm", "ack" if confirm == "ack" else "nack"]
    channel.set_qos.assert_awaited_once_with(prefetch_count=1)
    assert [c.args[0] for c in channel.get_queue.await_args_list] == [
        REQUEST_QUEUE,
        RETRY_QUEUE,
        DLQ,
    ]
    assert sent[0][1]["mandatory"] is True
    assert sent[0][0].delivery_mode == aio_pika.DeliveryMode.PERSISTENT
