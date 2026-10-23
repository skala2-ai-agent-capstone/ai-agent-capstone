import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ai_agents.cli.team_worker import main
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
