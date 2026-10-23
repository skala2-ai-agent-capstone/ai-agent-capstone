import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIStatusError, APITimeoutError
from pydantic import SecretStr
from test_stored_final_evaluation import JOB, StoredGateway, workflow

from ai_agents.evaluation_agent.final.validation import EvaluationError
from ai_agents.integrations.mcp_client import MCPError
from ai_agents.orchestration.final_worker import (
    DLQ,
    RESULT_QUEUE,
    RETRY_QUEUE,
    FinalEvaluationWorker,
    FinalJobExecutor,
    failure_code,
)
from ai_agents.shared.final_job_contracts import FinalEvaluationEvent

EVENT = FinalEvaluationEvent(message_id="m1", **JOB.model_dump())


def worker(execute, tmp_path, **kwargs):
    return FinalEvaluationWorker(
        execute, trace_dir=tmp_path / "traces", model="test-model",
        code_provenance={"git_commit": "2b14be1"}, **kwargs,
    )


@pytest.mark.asyncio
async def test_completion_publish_failure_redelivery_reuses_saved_checkpoint(tmp_path):
    gateway = StoredGateway()
    run, client = workflow(gateway, tmp_path / "checkpoints")
    async def execute(event):
        return await run.run(JOB)

    instance = worker(execute, tmp_path)
    ack, sent = AsyncMock(), []

    async def publish(queue, payload, code):
        sent.append((queue, deepcopy(payload), code))
        if len(sent) == 1:
            raise ConnectionError("sensitive detail")

    with pytest.raises(ConnectionError):
        await instance.handle(EVENT.model_dump_json().encode(), publish=publish, ack=ack)
    ack.assert_not_awaited()
    assert len(gateway.saved) == 2
    result = await instance.handle(EVENT.model_dump_json().encode(), publish=publish, ack=ack)
    assert result["status"] == "draft_and_coaching_saved"
    assert sent[0] == sent[1] and sent[1][0] == RESULT_QUEUE
    assert sent[1][1]["draft_revision"] == sent[1][1]["coaching_revision"] == 1
    assert "checkpoint" not in sent[1][1] and "receipts" not in sent[1][1]
    assert client.responses.parse.await_count == 1
    assert len([t for _, t, _ in gateway.calls if t.startswith("save_")]) == 2
    ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt,queue", [(0, RETRY_QUEUE), (3, DLQ), (4, DLQ)])
async def test_bounded_retries_keep_job_identity(tmp_path, attempt, queue):
    execute = AsyncMock(side_effect=MCPError("TRANSPORT_ERROR", "save_coaching"))
    published, ack = AsyncMock(), AsyncMock()
    event = EVENT.model_copy(update={"attempt": attempt})
    result = await worker(execute, tmp_path).handle(
        event.model_dump_json().encode(), publish=published, ack=ack,
    )
    assert result["status"] == ("retry" if queue == RETRY_QUEUE else "dlq")
    destination, payload, _ = published.await_args.args
    assert destination == queue
    assert payload["operation_id"] == EVENT.operation_id
    assert payload["message_id"] == EVENT.message_id
    assert payload["attempt"] == (attempt + 1 if queue == RETRY_QUEUE else attempt)
    assert execute.await_count == (0 if attempt == 4 else 1)
    ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'{"token":"SECRET"}', b"SECRET", b"x" * 16385,
    EVENT.model_dump_json().replace('"attempt":0', '"attempt":true').encode(),
    json.dumps({**EVENT.model_dump(), "conversation": "SECRET"}).encode(),
])
async def test_malformed_input_never_echoes_body(tmp_path, body):
    execute, publish, ack = AsyncMock(), AsyncMock(), AsyncMock()
    result = await worker(execute, tmp_path).handle(body, publish=publish, ack=ack)
    assert result == {"status": "dlq", "code": "INVALID_EVENT"}
    assert "SECRET" not in repr(publish.await_args)
    execute.assert_not_awaited()
    ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    MCPError("FORBIDDEN", "get_evaluation_context"),
    MCPError("UNTRUSTED_SECRET", "get_evaluation_context"),
    EvaluationError("SECRET"), RuntimeError("SECRET"),
])
async def test_nonretryable_failures_are_sanitized(tmp_path, exc):
    publish, ack = AsyncMock(), AsyncMock()
    result = await worker(AsyncMock(side_effect=exc), tmp_path).handle(
        EVENT.model_dump_json().encode(), publish=publish, ack=ack,
    )
    assert result["status"] == "dlq" and publish.await_args.args[0] == DLQ
    traces = "".join(path.read_text() for path in (tmp_path / "traces").glob("*.jsonl"))
    assert "SECRET" not in traces + repr(publish.await_args) + repr(result)


@pytest.mark.asyncio
async def test_failed_retry_publication_never_acks(tmp_path):
    ack = AsyncMock()
    with pytest.raises(ConnectionError):
        await worker(AsyncMock(side_effect=MCPError("TRANSPORT_ERROR", "read")), tmp_path).handle(
            EVENT.model_dump_json().encode(),
            publish=AsyncMock(side_effect=ConnectionError()), ack=ack,
        )
    ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_ack_or_retry(tmp_path):
    publish, ack = AsyncMock(), AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await worker(AsyncMock(side_effect=asyncio.CancelledError()), tmp_path).handle(
            EVENT.model_dump_json().encode(), publish=publish, ack=ack,
        )
    publish.assert_not_awaited()
    ack.assert_not_awaited()


@pytest.mark.parametrize("status,retry", [(400, False), (401, False), (429, True), (503, True)])
def test_wrapped_model_errors(status, retry):
    cause = APIStatusError(
        "SECRET", response=httpx.Response(status, request=httpx.Request("POST", "http://test")),
        body=None,
    )
    exc = EvaluationError("SECRET")
    exc.__cause__ = cause
    assert failure_code(exc)[1] is retry
    assert failure_code(APITimeoutError(request=httpx.Request("POST", "http://test")))[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("status", "confirmed"), ("evaluation_id", "another-evaluation"),
    ("review_required", "false"), ("receipts", {}),
])
async def test_incomplete_or_wrong_result_never_announced(tmp_path, field, value):
    result = {
        "status": "draft_and_coaching_saved", "evaluation_id": EVENT.evaluation_id,
        "review_required": False, "receipts": {
            "save_evaluation_draft": {"evaluation_id": EVENT.evaluation_id, "draft_revision": 1},
            "save_coaching": {"evaluation_id": EVENT.evaluation_id, "coaching_revision": 1},
        },
    }
    result[field] = value
    publish = AsyncMock()
    await worker(AsyncMock(return_value=result), tmp_path).handle(
        EVENT.model_dump_json().encode(), publish=publish, ack=AsyncMock(),
    )
    assert publish.await_args.args[0] == DLQ


@pytest.mark.asyncio
async def test_executor_gets_token_for_each_event_and_uses_mcp(monkeypatch, tmp_path):
    module = "ai_agents.orchestration.final_worker"
    tokens, jobs = [], []

    class Gateway:
        def __init__(self, token, **kwargs):
            tokens.append(token)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    class Workflow:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, job):
            jobs.append(job)
            return {"ok": True}

    provider = AsyncMock(side_effect=[SecretStr("first-secret"), SecretStr("second-secret")])
    monkeypatch.setattr(f"{module}.FinalMCPGateway", Gateway)
    monkeypatch.setattr(f"{module}.FinalEvaluationWorkflow", Workflow)
    executor = FinalJobExecutor(
        provider, object(), model="test", business_url="http://business", rag_url="http://rag",
        checkpoint_dir=tmp_path, code_provenance={},
    )
    await executor(EVENT)
    other = EVENT.model_copy(update={"evaluation_id": "EVAL-OTHER"})
    await executor(other)
    assert tokens == ["first-secret", "second-secret"]
    assert jobs[0].evaluation_id != jobs[1].evaluation_id
    assert provider.await_args_list[1].args == (other,)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["ack", "nack", "missing"])
async def test_rabbit_publisher_confirmation_and_manual_ack(monkeypatch, confirm):
    aio_pika = pytest.importorskip("aio_pika")
    from pamqp.commands import Basic

    from ai_agents.integrations.final_rabbit import consume_final_evaluations
    from ai_agents.orchestration.final_worker import REQUEST_QUEUE

    calls = []

    class Incoming:
        body, processed = b"{}", False

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

        async def __aexit__(self, *exc):
            pass

        def __aiter__(self):
            async def messages():
                yield Incoming()
            return messages()

    async def publish(message, **kwargs):
        assert kwargs["mandatory"] and message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        calls.append("confirm")
        return {"ack": Basic.Ack(), "nack": Basic.Nack(), "missing": None}[confirm]

    channel = SimpleNamespace(
        set_qos=AsyncMock(), get_queue=AsyncMock(return_value=Queue()),
        default_exchange=SimpleNamespace(publish=publish),
    )

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def channel(self, **kwargs):
            assert kwargs == {"publisher_confirms": True, "on_return_raises": True}
            return channel

    async def handle(body, *, publish, ack):
        await publish(RESULT_QUEUE, {"message_id": "m1"}, "SAVED")
        await ack()

    monkeypatch.setattr(aio_pika, "connect_robust", AsyncMock(return_value=Connection()))
    monkeypatch.setattr("ai_agents.integrations.final_rabbit.asyncio.sleep", AsyncMock())
    await consume_final_evaluations("amqp://unused", SimpleNamespace(handle=handle))
    assert calls == ["confirm", "ack" if confirm == "ack" else "nack"]
    assert [c.args[0] for c in channel.get_queue.await_args_list] == [
        REQUEST_QUEUE, RETRY_QUEUE, DLQ, RESULT_QUEUE,
    ]
