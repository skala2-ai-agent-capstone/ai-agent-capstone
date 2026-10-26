import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from ai_agents.cli.team_evaluation import main
from ai_agents.evaluation_agent.team.agent import TeamAgent, TeamEvaluationError
from ai_agents.evaluation_agent.team.report import ACTIONS, build_report, metrics_for
from ai_agents.integrations.mcp_client import MCPError
from ai_agents.integrations.team_mcp import TeamMCPGateway
from ai_agents.orchestration.team_evaluation import TeamEvaluationWorkflow, build_save_request
from ai_agents.shared.team_contracts import TeamDecision, TeamSnapshot
from ai_agents.validation.models import Dataset
from ai_agents.validation.quality import (
    QualityConfig,
    check_quality_dataset,
    run_quality_with_client,
)
from ai_agents.validation.quality_reviews import (
    checked_reviews,
    compare_quality,
    export_quality_report,
    review_template,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = QualityConfig(model="team-test-model", base_url="http://team-test.invalid/v1")


def snapshot():
    return TeamSnapshot.model_validate_json((ROOT / "samples/team/snapshot.json").read_text())


def dataset():
    return Dataset.model_validate_json((ROOT / "samples/team/quality-v1.json").read_text())


def decision_for(rows):
    strengths, improvements = [], []
    for row in rows:
        if row["band"] == "strength":
            strengths.append(row["metric_id"])
        else:
            action = next(k for k, v in ACTIONS.items() if v[0] == row["criterion"])
            improvements.append({"metric_id": row["metric_id"], "action_id": action})
    return {"strengths": strengths[:3], "improvements": improvements[:3]}


def good_report(value):
    rows = [m.model_dump(mode="json") for m in metrics_for(value)]
    return build_report(value, TeamDecision.model_validate(decision_for(rows)))


def transport(calls, *, fail_first=False, invalid=False, status=200, finish="stop"):
    def handle(request):
        assert request.url.host == "team-test.invalid"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["x-stage"] == "evaluate"
        assert "x-user-id" not in request.headers and "x-work-id" not in request.headers
        body = json.loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "PRIVATE UPSTREAM ERROR"}})
        payload = json.loads(body["messages"][1]["content"])
        assert set(payload) == {"metrics", "actions"}
        assert not any(k in json.dumps(payload) for k in (
            "team_id", "request_id", "snapshot_id", "period_start", "rubric_sha256", "members",
        ))
        choice = decision_for(payload["metrics"])
        if invalid or (fail_first and len(calls) == 1):
            choice = {"strengths": ["g999.security"], "improvements": []}
        return httpx.Response(200, json={
            "id": "team-test-response", "object": "chat.completion", "created": 1,
            "model": CONFIG.model, "choices": [{"index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": json.dumps(choice)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        })
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("change", [
    lambda v: v.update(user_id="private"),
    lambda v: v.update(min_members=1),
    lambda v: v.update(release_approved=False),
    lambda v: v.update(period_end=v["period_start"]),
    lambda v: v.update(min_members=9),
    lambda v: v["groups"][0].update(members=4),
    lambda v: v["groups"][0].update(finalized_evaluations=6),
    lambda v: v["groups"][0].update(employee_scores=[1, 2]),
    lambda v: v["groups"][0]["cells"][0]["statistics"].update(scored_members=4),
    lambda v: v["groups"][0]["cells"][0]["statistics"].update(scored_evaluations=11),
    lambda v: v["groups"][0]["cells"][0]["statistics"].update(score_sum=21),
    lambda v: v["groups"][0]["cells"][0]["statistics"].update(score_sum=True),
    lambda v: v["groups"][0]["cells"][0].update(quote="private conversation"),
    lambda v: v["groups"][0]["cells"][0].update(criterion="security"),
    lambda v: v["groups"][0]["cells"][3]["statistics"].update(score_sum=19),
    lambda v: v["groups"].append(deepcopy(v["groups"][0])),
])
def test_reject_invalid_or_identifiable_inputs(change):
    value = snapshot().model_dump(mode="json")
    change(value)
    with pytest.raises(ValidationError):
        TeamSnapshot.model_validate(value)


def test_calculation_missing_values_and_rubric_isolation():
    data = dataset()
    value = TeamSnapshot.model_validate(data.examples[5].inputs["snapshot"])
    report = good_report(value)
    assert len(report.metrics) == 4
    assert not any(m.criterion == "verification" for m in report.metrics)
    definition = next(m for m in report.metrics if m.criterion == "problem_definition")
    assert definition.mean_score == 2 and definition.coverage_percent == 50
    assert definition.scored_evaluations == 5
    value = TeamSnapshot.model_validate(data.examples[6].inputs["snapshot"])
    report = good_report(value)
    assert len(report.metrics) == 15
    assert len({m.metric_id for m in report.metrics}) == 15
    assert {m.rubric_version for m in report.metrics} == {"1", "2"}
    assert {m.work_type for m in report.metrics} == {"proposal", "quote"}
    value.groups.reverse()
    assert metrics_for(value) == report.metrics
    assert "total_score" not in report.model_dump()
    zero = good_report(TeamSnapshot.model_validate(data.examples[2].inputs["snapshot"]))
    assert all(m.mean_score == 0 for m in zero.metrics)
    assert zero.status == "generated" and not zero.strengths


@pytest.mark.asyncio
@pytest.mark.parametrize("index,status", [(3, "withheld"), (4, "insufficient_data")])
async def test_no_model_call_without_released_evidence(index, status):
    value = TeamSnapshot.model_validate(dataset().examples[index].inputs["snapshot"])
    result = await TeamAgent(None, "unused").generate(value)
    assert result.status == status
    assert not result.metrics and not result.strengths and not result.improvements


@pytest.mark.parametrize("choice", [
    {"strengths": ["g999.security"], "improvements": []},
    {"strengths": ["g1.security", "g1.security"], "improvements": []},
    {"strengths": ["g1.verification"], "improvements": []},
    {"strengths": [], "improvements": []},
    {"strengths": ["g1.security"], "improvements": [
        {"metric_id": "g1.verification", "action_id": "mask_before_input"}]},
    {"strengths": ["g1.security"], "improvements": [], "employee_name": "invented"},
])
def test_reject_unfounded_claims_and_free_text(choice):
    with pytest.raises(ValueError):
        build_report(snapshot(), TeamDecision.model_validate(choice))


@pytest.mark.asyncio
async def test_real_sdk_transport_retry_and_safe_errors():
    calls = []
    async with AsyncOpenAI(base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
                           http_client=httpx.AsyncClient(transport=transport(
                               calls, fail_first=True,
                           ))) as client:
        report = await TeamAgent(client, CONFIG.model).generate(snapshot())
    assert report.status == "generated" and len(calls) == 2
    assert "g999" not in json.dumps(calls[1])
    assert "개인의 역량" in report.limitations[0]


@pytest.mark.asyncio
async def test_drops_metric_placed_in_wrong_band():
    # 강점 후보 하나뿐일 때 모델이 같은 지표를 개선에도 넣은 실제 응답(서버 llm_exchanges 376)
    value = snapshot().model_dump(mode="json")
    for cell in value["groups"][0]["cells"]:
        if cell["criterion"] != "security":
            cell["statistics"] = None
    value = TeamSnapshot.model_validate(value)
    reply = {"strengths": ["g1.security"], "improvements": [
        {"metric_id": "g1.security", "action_id": "mask_before_input"},
        {"metric_id": "g1.security", "action_id": "check_sharing_scope"}]}
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={
            "id": "team-test-response", "object": "chat.completion", "created": 1,
            "model": CONFIG.model, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(reply)}}],
        })

    async with AsyncOpenAI(base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
                           http_client=httpx.AsyncClient(
                               transport=httpx.MockTransport(handle))) as client:
        report = await TeamAgent(client, CONFIG.model).generate(value)
    assert report.status == "generated" and len(calls) == 1
    assert [s.metric_id for s in report.strengths] == ["g1.security"] and report.improvements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("options,code,count,retryable", [
    ({"invalid": True}, "INVALID_TEAM_DECISION", 2, False),
    ({"status": 503}, "LLM_UNAVAILABLE", 1, True),
    ({"status": 401}, "LLM_REJECTED", 1, False),
    ({"finish": "length"}, "LLM_REJECTED", 1, False),
])
async def test_failure_classification(options, code, count, retryable):
    calls = []
    async with AsyncOpenAI(base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
                           http_client=httpx.AsyncClient(transport=transport(
                               calls, **options,
                           ))) as client:
        with pytest.raises(TeamEvaluationError) as error:
            await TeamAgent(client, CONFIG.model).generate(snapshot())
    assert error.value.code == code and error.value.retryable == retryable
    assert "PRIVATE" not in str(error.value)
    assert len(calls) == count


@pytest.mark.asyncio
async def test_cancellation_propagates():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=asyncio.CancelledError()),
    )))
    with pytest.raises(asyncio.CancelledError):
        await TeamAgent(client, CONFIG.model).generate(snapshot())


def test_save_payload_rejects_tampering_and_has_stable_key():
    value = snapshot()
    report = good_report(value)
    first = build_save_request(value, report)
    assert first == build_save_request(value, report)
    for key, changed in (("summary", "invented"), ("snapshot_id", UUID(int=999))):
        corrupt = report.model_copy(update={key: changed})
        with pytest.raises(ValueError):
            build_save_request(value, corrupt)
    corrupt = report.model_copy(deep=True)
    corrupt.metrics[0].mean_score = 2.0
    with pytest.raises(ValueError):
        build_save_request(value, corrupt)
    decision = TeamDecision.model_validate(decision_for([
        m.model_dump(mode="json") for m in metrics_for(value)
    ]))
    decision.improvements[0].action_id = "request_evidence"
    second = build_save_request(value, build_report(value, decision))
    assert first.idempotency_key == second.idempotency_key
    assert first.report_sha256 != second.report_sha256  # server must reject key/payload conflict


@pytest.mark.asyncio
async def test_mcp_prepare_save_replay_and_scope_validation():
    value = snapshot()
    saved = []

    async def call(server, tool, args):
        assert server == "business"
        if tool == "get_team_evaluation_snapshot":
            assert set(args) == {"request_id"}
            return {"ok": True, "data": value.model_dump(mode="json")}
        assert tool == "save_team_evaluation_report"
        saved.append(args)
        return {"ok": True, "data": {
            **{k: args[k] for k in ("request_id", "snapshot_id", "report_sha256")},
            "revision": 1, "status": "saved",
        }}

    gateway = SimpleNamespace(call=AsyncMock(side_effect=call))
    agent = SimpleNamespace(generate=AsyncMock(return_value=good_report(value)))
    workflow = TeamEvaluationWorkflow(gateway, agent)
    original, report = await workflow.prepare(value.request_id)
    await workflow.save(original, report)
    await workflow.save(original, report)
    assert saved[0] == saved[1] and agent.generate.await_count == 1
    with pytest.raises(ValueError):
        await workflow.prepare(UUID(int=999))
    gateway.call.side_effect = None
    gateway.call.return_value = {"ok": True, "data": {
        "request_id": str(value.request_id), "snapshot_id": str(UUID(int=999)),
        "report_sha256": saved[0]["report_sha256"], "revision": 1, "status": "saved",
    }}
    with pytest.raises(ValueError):
        await workflow.save(original, report)


@pytest.mark.asyncio
async def test_gateway_cannot_read_individual_records():
    gateway = TeamMCPGateway("secret", "http://business.invalid/mcp")
    assert set(gateway._urls) == {"business"}
    gateway._call = AsyncMock(return_value={"ok": True, "data": {}})
    for server, tool in (("business", "get_conversation"), ("business", "get_work_case"),
                         ("business", "save_evaluation_draft"), ("rag", "search_rubric")):
        with pytest.raises(MCPError, match="TOOL_NOT_ALLOWED"):
            await gateway.call(server, tool, {})
    assert gateway._call.await_count == 0
    await gateway.call("business", "get_team_evaluation_snapshot", {})
    assert gateway._call.await_count == 1


@pytest.mark.asyncio
async def test_quality_dataset_real_sdk_and_comparison(tmp_path):
    data = dataset()
    assert check_quality_dataset(data)["cases"] == 8
    calls = []
    async with AsyncOpenAI(base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
                           http_client=httpx.AsyncClient(transport=transport(calls))) as client:
        experiment = await run_quality_with_client(data, client, CONFIG,
            output_dir=tmp_path / "experiment", repo=ROOT, label="team-test")
    assert len(calls) == 6 and all(c.verdict == "passed" for c in experiment.cases)
    assert "expected_description" not in json.dumps(calls)
    assert experiment.manifest["model_settings"]["max_tokens"] == 1800
    report = export_quality_report(experiment, None, tmp_path / "report")
    assert report["generated"] == 8 and report["model_calls"] == 6
    assert report["tokens"] == {"input_tokens": 60, "output_tokens": 120}
    assert report["not_applicable"] == 2 and report["pending"] == 6
    sheet = review_template(experiment)
    assert sheet.cases[3].status == "not_applicable"
    sheet.cases[3].status = "pending"
    with pytest.raises(ValueError, match="call-free"):
        checked_reviews(experiment, sheet)
    assert report["reviewed"] == 0 and report["quality_mean"]["grounding"] is None
    comparison = compare_quality(experiment, experiment)
    assert comparison["paired_reviewed_case_ids"] == []
    traces = "".join(p.read_text() for p in (tmp_path / "experiment/traces").glob("*.jsonl"))
    assert "업무 시작 전에" not in traces and "test-secret" not in traces


def test_cli_check_schema_export_and_no_overwrite(tmp_path, capsys):
    assert main(["check", "--snapshot", str(ROOT / "samples/team/snapshot.json")]) == 0
    assert '"network_calls": 0' in capsys.readouterr().out
    assert main(["schemas"]) == 0
    assert "TeamSaveReceipt" in capsys.readouterr().out
    source = tmp_path / "report.json"
    source.write_text(good_report(snapshot()).model_dump_json())
    args = ["save-request", "--snapshot", str(ROOT / "samples/team/snapshot.json"),
            "--report", str(source), "--output", str(tmp_path / "save.json")]
    assert main(args) == 0 and main(args) == 2
    assert source.read_text() == good_report(snapshot()).model_dump_json()


def test_cli_runs_actual_sdk_writes_bound_local_result(tmp_path, monkeypatch, capsys):
    calls = []

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport(calls)))

    monkeypatch.setattr("ai_agents.cli.team_evaluation.AsyncOpenAI", client)
    monkeypatch.setenv("TEAM_LLM_BASE_URL", CONFIG.base_url)
    monkeypatch.setenv("TEAM_LLM_MODEL", CONFIG.model)
    monkeypatch.setenv("TEAM_LLM_API_KEY", "test-secret")
    folder = tmp_path / "local"
    args = ["run", "--snapshot", str(ROOT / "samples/team/snapshot.json"),
            "--output-dir", str(folder)]
    assert main(args) == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["persistence"] == "local_only"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["model"] == CONFIG.model and manifest["langsmith_requested"] is False
    assert "team-test.invalid" not in (folder / "manifest.json").read_text()
    value = json.loads((folder / "save-request.json").read_text())
    assert value == build_save_request(snapshot(), good_report(snapshot())).model_dump(mode="json")
    assert main(args) == 2 and len(calls) == 1


def test_cli_withheld_does_not_need_model_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_LLM_BASE_URL", "")
    monkeypatch.setenv("TEAM_LLM_MODEL", "")
    source = tmp_path / "withheld.json"
    source.write_text(json.dumps(dataset().examples[3].inputs["snapshot"]))
    folder = tmp_path / "withheld-result"
    assert main(["run", "--snapshot", str(source), "--output-dir", str(folder)]) == 0
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["model"] is None and manifest["status"] == "withheld"
