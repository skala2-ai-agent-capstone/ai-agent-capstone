import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_stored_final_evaluation import JOB, StoredGateway, workflow

from ai_agents.cli.feedback import main as feedback_main
from ai_agents.cli.final_worker import parser
from ai_agents.evaluation_agent.final.validation import EvaluationError
from ai_agents.integrations.final_tokens import FileFinalTokenProvider, FinalTokenError
from ai_agents.orchestration.final_evaluation import FinalEvaluationJob
from ai_agents.orchestration.final_worker import (
    FinalEvaluationWorker,
    FinalJobExecutor,
    LifecycleEventRequiresBackend,
    failure_code,
)
from ai_agents.shared.final_job_contracts import (
    FINAL_QUEUE_CONTRACTS,
    BackendEvaluationEvent,
)


def event_for(gateway=None):
    gateway = gateway or StoredGateway()
    return BackendEvaluationEvent(
        type="EVALUATE", message_id="outbox-1", evaluation_id=JOB.evaluation_id,
        company_id="C-TEST", task_id=gateway.target["work_id"],
        deliverable_version=gateway.target["artifact_version"],
        rubric_version=gateway.target["rubric_version"], attempt=0,
    )


def worker(execute, tmp_path):
    return FinalEvaluationWorker(execute, trace_dir=tmp_path / "traces", model="test",
                                 code_provenance={}, contract="backend-v1")


def test_stable_identity_excludes_delivery_and_retry_but_includes_scope():
    first = event_for()
    for change in ({"message_id": "outbox-2"}, {"attempt": 3}):
        second = first.model_copy(update=change)
        assert first.normalized().operation_id == second.normalized().operation_id
    for key in ("company_id", "task_id", "deliverable_version", "rubric_version", "evaluation_id"):
        other = first.model_copy(update={key: "different"})
        assert first.normalized().operation_id != other.normalized().operation_id


@pytest.mark.parametrize("change", [
    {"type": "RESUME"}, {"attempt": True}, {"attempt": -1}, {"message_id": ""},
    {"operation_id": "unnegotiated"}, {"rubric_version": 2}, {"raw_content": "PRIVATE"},
])
def test_strict_backend_message(change):
    with pytest.raises(ValidationError):
        BackendEvaluationEvent.model_validate({**event_for().model_dump(), **change})


@pytest.mark.asyncio
async def test_backend_redelivery_reuses_checkpoint_and_notification(tmp_path):
    gateway = StoredGateway()
    gateway.target["company_id"] = "C-TEST"
    execution, client = workflow(gateway, tmp_path / "checkpoints")

    async def execute(event):
        return await execution.run(FinalEvaluationJob(
            evaluation_id=event.evaluation_id, operation_id=event.operation_id,
        ), expected=event.backend_scope)

    instance = worker(execute, tmp_path)
    sent, ack = [], AsyncMock()

    async def publish(queue, payload, code):
        sent.append((queue, payload, code))
        if len(sent) == 1:
            raise ConnectionError("confirmation lost")

    event = event_for(gateway)
    with pytest.raises(ConnectionError):
        await instance.handle(event.model_dump_json().encode(), publish=publish, ack=ack)
    ack.assert_not_awaited()
    event.message_id = "outbox-republished"
    result = await instance.handle(event.model_dump_json().encode(), publish=publish, ack=ack)
    assert result["status"] == "draft_and_coaching_saved"
    assert sent[0] == sent[1] and sent[1][0] == "evaluation.results"
    assert sent[1][1]["type"] == "FINAL_EVALUATION_SAVED"
    assert client.responses.parse.await_count == 1 and len(gateway.saved) == 2
    assert not any(t == "request_review" for _, t, _ in gateway.calls)
    ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "company_id", "task_id", "deliverable_version", "rubric_version",
])
async def test_queue_scope_checked_against_mcp_before_model_and_saves(tmp_path, field):
    gateway = StoredGateway()
    gateway.target["company_id"] = "C-TEST"
    execution, client = workflow(gateway, tmp_path)
    expected = event_for(gateway).normalized().backend_scope
    expected = expected.model_copy(update={field: "mismatch"})
    with pytest.raises(EvaluationError):
        await execution.run(JOB, expected=expected)
    client.responses.parse.assert_not_awaited()
    assert not gateway.saved


@pytest.mark.asyncio
async def test_retry_preserves_backend_wire_shape(tmp_path):
    execute = AsyncMock(side_effect=FinalTokenError("TOKEN_EXPIRED", retryable=True))
    sent, ack = AsyncMock(), AsyncMock()
    original = event_for()
    result = await worker(execute, tmp_path).handle(
        original.model_dump_json().encode(), publish=sent, ack=ack,
    )
    queue, payload, code = sent.await_args.args
    assert queue == "evaluation.retry" and code == "TOKEN_EXPIRED"
    assert payload == {**original.model_dump(), "attempt": 1}
    assert result["status"] == "retry"
    assert "backend_scope" not in payload and "operation_id" not in payload
    ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_does_not_re_evaluate_fail_or_ack(tmp_path):
    execute, sent, ack = AsyncMock(), AsyncMock(), AsyncMock()
    with pytest.raises(LifecycleEventRequiresBackend, match="RESUME_REQUIRES_BACKEND"):
        await worker(execute, tmp_path).handle(
            b'{"type":"RESUME","evaluation_id":"EVAL","reason":"PRIVATE"}',
            publish=sent, ack=ack,
        )
    execute.assert_not_awaited()
    sent.assert_not_awaited()
    ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_backend_event_dlq_is_sanitized(tmp_path):
    sent = AsyncMock()
    await worker(AsyncMock(), tmp_path).handle(
        b'{"type":"EVALUATE","token":"PRIVATE"}', publish=sent, ack=AsyncMock(),
    )
    assert sent.await_args.args[0] == "evaluation.dlq"
    assert "PRIVATE" not in repr(sent.await_args)


@pytest.mark.asyncio
async def test_executor_propagates_expected_scope(monkeypatch, tmp_path):
    module = "ai_agents.orchestration.final_worker"
    instance = SimpleNamespace(run=AsyncMock(return_value={}))
    gateway = AsyncMock()
    monkeypatch.setattr(module + ".FinalMCPGateway", lambda *a, **k: gateway)
    monkeypatch.setattr(module + ".FinalEvaluationWorkflow", lambda *a, **k: instance)
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"tokens": {JOB.evaluation_id: {
        "token": "private-token", "expires_at": "2099-01-01T00:00:00Z",
    }}}))
    executor = FinalJobExecutor(FileFinalTokenProvider(path), object(), model="test",
        business_url="http://business", rag_url="http://rag", checkpoint_dir=tmp_path,
        code_provenance={})
    event = event_for().normalized()
    await executor(event)
    assert instance.run.await_args.kwargs["expected"] == event.backend_scope


@pytest.mark.asyncio
async def test_file_token_refresh_expiry_and_sanitized_errors(tmp_path):
    path = tmp_path / "tokens.json"
    provider = FileFinalTokenProvider(path, now=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc))

    def write(token, expiry="2026-10-24T00:00:00Z", evaluation_id=JOB.evaluation_id):
        path.write_text(json.dumps({"tokens": {evaluation_id: {
            "token": token, "expires_at": expiry,
        }}}))

    for token in ("PRIVATE-FIRST", "PRIVATE-ROTATED"):
        write(token)
        secret = await provider(event_for().normalized())
        assert secret.get_secret_value() == token and token not in repr(secret)
    for expiry, expected_code in (("2026-10-22T00:00:00Z", "TOKEN_EXPIRED"),
                                  ("not-time", "TOKEN_FILE_INVALID")):
        write("PRIVATE", expiry)
        with pytest.raises(FinalTokenError) as exc:
            await provider(event_for().normalized())
        assert str(exc.value) == expected_code and "PRIVATE" not in str(exc.value)
        assert failure_code(exc.value)[1] == (expected_code == "TOKEN_EXPIRED")
    write("PRIVATE", evaluation_id="another-evaluation")
    with pytest.raises(FinalTokenError, match="TOKEN_NOT_READY"):
        await provider(event_for().normalized())
    path.unlink()
    with pytest.raises(FinalTokenError, match="TOKEN_FILE_UNAVAILABLE"):
        provider.read()


def test_cli_contract_and_token_options():
    args = parser().parse_args([
        "worker", "--contract", "backend-v1", "--token-file", "tokens.json",
    ])
    assert args.contract == "backend-v1" and args.token_provider is None
    assert FINAL_QUEUE_CONTRACTS[args.contract].results == "evaluation.results"
    with pytest.raises(SystemExit):
        parser().parse_args(["worker", "--token-file", "x", "--token-provider", "x:y"])


def test_feedback_preflight_reports_names_not_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ai_agents.cli.feedback.load_dotenv", lambda *a, **k: None)
    values = {
        "FEEDBACK_AMQP_URL": "amqp://test:PRIVATE@host:5672/",
        "FEEDBACK_MCP_URL": "http://host:8001/mcp", "FEEDBACK_MCP_TOKEN": "PRIVATE",
        "FEEDBACK_LLM_BASE_URL": "http://host:4000/v1", "FEEDBACK_LLM_MODEL": "model",
        "FEEDBACK_LLM_API_KEY": "PRIVATE",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    assert feedback_main(["check-config"]) == 0
    assert "PRIVATE" not in capsys.readouterr().out
    monkeypatch.delenv("FEEDBACK_MCP_TOKEN")
    monkeypatch.setenv("FEEDBACK_AMQP_URL", "amqp://user:PRIVATE@host:bad/")
    assert feedback_main(["check-config"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["missing"] == ["FEEDBACK_MCP_TOKEN"]
    assert result["invalid"] == ["FEEDBACK_AMQP_URL"] and result["network_calls"] == 0
    source = tmp_path / "event.json"
    source.write_text(json.dumps(dict(message_id="m1", type="LIVE_EVALUATE", live_eval_id="L1",
        company_id="C1", work_id="W1", requested_by="U1", from_seq=1, to_seq=10,
        rubric_version="v2", attempt=0)))
    assert feedback_main(["check-event", "--event", str(source)]) == 0
    value = json.loads(source.read_text())
    value["unnegotiated"] = "PRIVATE"
    source.write_text(json.dumps(value))
    assert feedback_main(["check-event", "--event", str(source)]) == 2
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_backend_transport_preserves_resume_and_stops(monkeypatch, tmp_path):
    aio_pika = pytest.importorskip("aio_pika")
    from ai_agents.integrations.final_rabbit import consume_final_evaluations

    message = SimpleNamespace(body=b'{"type":"RESUME"}', processed=False,
                              ack=AsyncMock(), nack=AsyncMock())

    class Queue:
        def iterator(self, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def __aiter__(self):
            async def stream():
                yield message
            return stream()

    channel = SimpleNamespace(set_qos=AsyncMock(), get_queue=AsyncMock(return_value=Queue()),
                              default_exchange=SimpleNamespace(publish=AsyncMock()))
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.channel.return_value = channel
    monkeypatch.setattr(aio_pika, "connect_robust", AsyncMock(return_value=connection))
    with pytest.raises(LifecycleEventRequiresBackend):
        await consume_final_evaluations("amqp://unused", worker(AsyncMock(), tmp_path))
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)
    channel.default_exchange.publish.assert_not_awaited()
    assert [c.args[0] for c in channel.get_queue.await_args_list] == [
        "evaluation.requests", "evaluation.retry", "evaluation.dlq", "evaluation.results",
    ]
