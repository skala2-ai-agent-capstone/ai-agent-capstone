import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from ai_agents.cli.team_worker import main
from ai_agents.evaluation_agent.team.agent import TeamEvaluationError
from ai_agents.evaluation_agent.team.report import ACTIONS, build_report, metrics_for
from ai_agents.integrations.final_tokens import FileFinalTokenProvider
from ai_agents.integrations.mcp_client import MCPError
from ai_agents.orchestration.team_worker import TeamJobExecutor, TeamWorker
from ai_agents.shared.team_contracts import TeamDecision, TeamSnapshot
from ai_agents.shared.team_job_contracts import DLQ, RETRY_QUEUE

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_EVENT = ROOT / "samples/integration/team-evaluate-v1.json"


def snapshot():
    return TeamSnapshot.model_validate_json((ROOT / "samples/team/snapshot.json").read_text())


def good_report(value):
    strengths, improvements = [], []
    for row in (m.model_dump(mode="json") for m in metrics_for(value)):
        if row["band"] == "strength":
            strengths.append(row["metric_id"])
        else:
            action = next(k for k, v in ACTIONS.items() if v[0] == row["criterion"])
            improvements.append({"metric_id": row["metric_id"], "action_id": action})
    return build_report(
        value,
        TeamDecision.model_validate({"strengths": strengths[:3], "improvements": improvements[:3]}),
    )


def event(attempt=0, request_id=None):
    value = json.loads(SAMPLE_EVENT.read_text())
    value["request_id"] = str(request_id or snapshot().request_id)
    value["attempt"] = attempt
    return json.dumps(value).encode()


class FakeMCP:
    """business-mcp 팀 도구 두 개. save_errors에 넣은 오류를 차례로 낸다."""

    def __init__(self, save_errors=()):
        self.calls, self.tokens, self.save_errors = [], [], list(save_errors)

    def factory(self, token, business_url):
        self.tokens.append(token)
        assert business_url == "http://business/mcp"
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, server, tool, args):
        assert server == "business"
        self.calls.append(tool)
        if tool == "get_team_evaluation_snapshot":
            return {"ok": True, "data": snapshot().model_dump(mode="json")}
        if self.save_errors:
            raise self.save_errors.pop(0)
        return {
            "ok": True,
            "data": {
                **{k: args[k] for k in ("request_id", "snapshot_id", "report_sha256")},
                "revision": 1,
                "status": "saved",
            },
        }


def token_file(path, request_id=None, expires="2099-01-01T00:00:00Z"):
    tokens = (
        {}
        if request_id is None
        else {str(request_id): {"token": "TEAM-SECRET", "expires_at": expires}}
    )
    path.write_text(json.dumps({"tokens": tokens}))


def worker_for(tmp_path, mcp, agent, max_retries=3):
    tokens = tmp_path / "team-tokens.json"
    if not tokens.exists():
        token_file(tokens, snapshot().request_id)
    executor = TeamJobExecutor(
        FileFinalTokenProvider(tokens, key="request_id"),
        agent,
        business_url="http://business/mcp",
        checkpoint_dir=tmp_path / "checkpoints",
        gateway_factory=mcp.factory,
    )
    return TeamWorker(
        executor,
        trace_dir=tmp_path / "traces",
        model="team-test-model",
        code_provenance={},
        max_retries=max_retries,
    )


async def run(worker, body):
    published, ack = [], AsyncMock()

    async def publish(destination, payload, code):
        published.append((destination, payload, code))

    result = await worker.handle(body, publish=publish, ack=ack)
    ack.assert_awaited_once()
    return result, published


@pytest.mark.asyncio
async def test_saves_report_with_request_token_and_acks_without_publishing(tmp_path):
    mcp, agent = (
        FakeMCP(),
        SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot()))),
    )

    result, published = await run(worker_for(tmp_path, mcp, agent), event())

    assert (
        result["status"] == "saved"
        and result["report_status"] == "generated"
        and not result["resumed"]
    )
    assert published == []  # 완료는 MCP 저장으로 끝난다(결과 큐 없음)
    assert mcp.tokens == ["TEAM-SECRET"]
    assert mcp.calls == ["get_team_evaluation_snapshot", "save_team_evaluation_report"]
    checkpoint = tmp_path / "checkpoints" / f"{snapshot().request_id}.json"
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert "TEAM-SECRET" not in checkpoint.read_text()


@pytest.mark.asyncio
async def test_lost_save_is_retried_from_checkpoint_without_new_model_call(tmp_path):
    mcp = FakeMCP(save_errors=[MCPError("TRANSPORT_ERROR", "save_team_evaluation_report")])
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot())))
    worker = worker_for(tmp_path, mcp, agent)

    result, published = await run(worker, event())
    assert result["status"] == "retry" and result["code"] == "MCP_UNAVAILABLE"
    destination, payload, code = published[0]
    assert (destination, code, payload["attempt"]) == (RETRY_QUEUE, "MCP_UNAVAILABLE", 1)
    assert set(payload) == {"message_id", "type", "request_id", "company_id", "attempt"}

    result, published = await run(worker, json.dumps(payload).encode())
    assert result["status"] == "saved" and result["resumed"] and published == []
    assert agent.generate.await_count == 1
    assert mcp.calls.count("get_team_evaluation_snapshot") == 1


@pytest.mark.asyncio
async def test_missing_token_waits_for_backend_then_succeeds(tmp_path):
    token_file(tmp_path / "team-tokens.json")  # 처리 중인 요청이 없을 때 백엔드가 쓰는 빈 목록
    mcp, agent = (
        FakeMCP(),
        SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot()))),
    )
    worker = worker_for(tmp_path, mcp, agent)

    result, published = await run(worker, event())
    assert result["code"] == "TOKEN_NOT_READY" and published[0][0] == RETRY_QUEUE
    assert mcp.calls == []

    token_file(tmp_path / "team-tokens.json", snapshot().request_id)
    result, _ = await run(worker, json.dumps(published[0][1]).encode())
    assert result["status"] == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (MCPError("VERSION_CONFLICT", "save_team_evaluation_report"), "REPORT_CONFLICT"),
        (MCPError("FORBIDDEN", "save_team_evaluation_report"), "TOKEN_REJECTED"),
        (MCPError("SOMETHING_NEW", "save_team_evaluation_report"), "MCP_REJECTED"),
    ],
)
async def test_non_retryable_failures_go_to_dlq_with_original_event(tmp_path, error, code):
    mcp, agent = (
        FakeMCP(save_errors=[error]),
        SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot()))),
    )

    result, published = await run(worker_for(tmp_path, mcp, agent), event())

    assert result == {**result, "status": "dlq", "code": code}
    destination, payload, header = published[0]
    assert (destination, header, payload["attempt"], payload["request_id"]) == (
        DLQ,
        code,
        0,
        str(snapshot().request_id),
    )


@pytest.mark.asyncio
async def test_retry_limit_sends_retryable_failure_to_dlq(tmp_path):
    mcp = FakeMCP(save_errors=[MCPError("TEMPORARY_FAILURE", "save_team_evaluation_report")])
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot())))

    result, published = await run(worker_for(tmp_path, mcp, agent, max_retries=3), event(attempt=3))
    assert (result["status"], published[0][0], published[0][2]) == ("dlq", DLQ, "MCP_UNAVAILABLE")

    result, published = await run(worker_for(tmp_path, mcp, agent, max_retries=3), event(attempt=4))
    assert (result["code"], published[0][0]) == ("RETRY_EXHAUSTED", DLQ)


@pytest.mark.asyncio
async def test_invalid_event_keeps_only_request_id_in_dlq(tmp_path):
    mcp, agent = FakeMCP(), SimpleNamespace(generate=AsyncMock())
    worker = worker_for(tmp_path, mcp, agent)
    rid = "0b7d8a3e-6f1c-4c9a-9f5e-2a1b3c4d5e6f"

    result, published = await run(
        worker, json.dumps({"type": "WRONG", "request_id": rid, "secret": "x"}).encode()
    )
    assert result == {"status": "dlq", "code": "INVALID_EVENT"}
    destination, payload, code = published[0]
    assert (destination, code, payload["request_id"], payload["type"]) == (
        DLQ,
        "INVALID_EVENT",
        rid,
        "INVALID_TEAM_EVALUATE",
    )
    assert "secret" not in payload

    _, published = await run(worker, b"not json")
    assert "request_id" not in published[0][1]
    assert mcp.calls == [] and agent.generate.await_count == 0

    # 깊게 중첩된 본문도 예외로 빠져나가지 않고 DLQ로 보낸 뒤 ack한다(아니면 무한 재전달)
    deep = b"[" * 20000 + b"]" * 20000
    for body in (deep, deep[:12000]):
        result, published = await run(worker, body)
        assert result == {"status": "dlq", "code": "INVALID_EVENT"}
        assert "request_id" not in published[0][1]


class DropConnection(httpx2.AsyncBaseTransport):
    """tool 이름이 든 요청(그 도구 호출 POST)에서 연결을 끊는다. 서버 재시작을 흉내 낸다."""

    def __init__(self, inner, tool=None):
        self.inner, self.tool = inner, tool

    async def handle_async_request(self, request):
        if self.tool and self.tool.encode() in request.content:
            raise httpx2.ConnectError("connection refused", request=request)
        return await self.inner.handle_async_request(request)


class TeamServer:
    """팀 도구 두 개를 실제 MCP SDK 서버로 띄운다.

    FakeMCP와 달리 클라이언트 task group이 블록 안 예외를 ExceptionGroup으로 감싸므로,
    운영과 같은 오류 경로로 Worker의 분류·재시도를 확인할 수 있다.
    """

    def __init__(self, save_error=None, drop_connection_on=None):
        self.drop_connection_on = drop_connection_on
        server = MCPServer("team-test")
        meta = {"run_id": "run-team", "snapshot": {"request_id": str(snapshot().request_id)}}

        @server.tool()
        async def get_team_evaluation_snapshot(request_id: str) -> dict:
            return {"ok": True, "data": snapshot().model_dump(mode="json"), "meta": meta}

        @server.tool()
        async def save_team_evaluation_report(
            request_id: str, snapshot_id: str, idempotency_key: str, report_sha256: str,
            report: dict,
        ) -> dict:
            if save_error:
                return {"ok": False, "error": {"code": save_error}}
            data = {"request_id": request_id, "snapshot_id": snapshot_id,
                    "report_sha256": report_sha256, "revision": 1, "status": "saved"}
            return {"ok": True, "data": data, "meta": meta}

        self.app = server.streamable_http_app(
            json_response=True, stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    def worker(self, tmp_path, monkeypatch, agent):
        def http_client(headers):
            return httpx2.AsyncClient(transport=DropConnection(
                httpx2.ASGITransport(app=self.app), self.drop_connection_on), headers=headers)

        monkeypatch.setattr("ai_agents.integrations.mcp_client.create_mcp_http_client", http_client)
        tokens = tmp_path / "team-tokens.json"
        token_file(tokens, snapshot().request_id)
        executor = TeamJobExecutor(
            FileFinalTokenProvider(tokens, key="request_id"), agent,
            business_url="http://testserver/mcp", checkpoint_dir=tmp_path / "checkpoints",
        )
        return TeamWorker(executor, trace_dir=tmp_path / "traces", model="team-test-model",
                          code_provenance={})

    async def run(self, worker, body):
        async with self.app.router.lifespan_context(self.app):
            return await run(worker, body)


@pytest.mark.asyncio
async def test_real_mcp_gateway_saves_report(tmp_path, monkeypatch):
    server = TeamServer()
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot())))

    result, published = await server.run(server.worker(tmp_path, monkeypatch, agent), event())

    assert result["status"] == "saved" and published == []


@pytest.mark.asyncio
async def test_real_mcp_gateway_keeps_retryable_model_failure(tmp_path, monkeypatch):
    """LLM 일시 장애는 MCP 연결 블록 안에서 난다. 감싸진 채로 나가면 바로 DLQ에 갔다."""
    server = TeamServer()
    agent = SimpleNamespace(
        generate=AsyncMock(side_effect=TeamEvaluationError("LLM_UNAVAILABLE", retryable=True)))

    result, published = await server.run(server.worker(tmp_path, monkeypatch, agent), event())

    assert result["status"] == "retry" and result["code"] == "LLM_UNAVAILABLE"
    destination, payload, code = published[0]
    assert (destination, code, payload["attempt"]) == (RETRY_QUEUE, "LLM_UNAVAILABLE", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mcp_code", "code"), [
    ("NOT_FOUND", "REQUEST_NOT_FOUND"),
    ("VERSION_CONFLICT", "REPORT_CONFLICT"),
    ("FORBIDDEN", "TOKEN_REJECTED"),
])
async def test_real_mcp_gateway_keeps_mcp_error_codes(tmp_path, monkeypatch, mcp_code, code):
    server = TeamServer(save_error=mcp_code)
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot())))

    result, published = await server.run(server.worker(tmp_path, monkeypatch, agent), event())

    assert result == {**result, "status": "dlq", "code": code}
    assert (published[0][0], published[0][2]) == (DLQ, code)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_team_evaluation_snapshot", "save_team_evaluation_report"])
async def test_real_mcp_gateway_retries_dropped_connection(tmp_path, monkeypatch, tool):
    """도구 호출 중 연결이 끊기면 블록은 SDK가 낸 CancelledError만 받는다. 그대로 올리면 Worker와
    소비 루프의 except Exception을 빠져나가 ack 없이 프로세스가 죽는다. 재시도로 처리해야 한다."""
    server = TeamServer(drop_connection_on=tool)
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(snapshot())))

    result, published = await server.run(server.worker(tmp_path, monkeypatch, agent), event())

    assert result["status"] == "retry" and result["code"] == "MCP_UNAVAILABLE"
    assert (published[0][0], published[0][1]["attempt"]) == (RETRY_QUEUE, 1)


@pytest.mark.asyncio
async def test_tampered_checkpoint_is_not_saved(tmp_path):
    mcp, agent = FakeMCP(), SimpleNamespace(generate=AsyncMock())
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / f"{snapshot().request_id}.json").write_text('{"snapshot": {}, "report": {}}')

    result, published = await run(worker_for(tmp_path, mcp, agent), event())

    assert (result["code"], published[0][0]) == ("CHECKPOINT_INVALID", DLQ)
    assert "save_team_evaluation_report" not in mcp.calls


def test_cli_check_event_and_schema(capsys):
    assert main(["check-event", "--event", str(SAMPLE_EVENT)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "status": "valid",
        "request_id": str(UUID("0b7d8a3e-6f1c-4c9a-9f5e-2a1b3c4d5e6f")),
        "network_calls": 0,
    }
    assert main(["schemas"]) == 0
    assert "TeamEvaluateEvent" in json.loads(capsys.readouterr().out)
