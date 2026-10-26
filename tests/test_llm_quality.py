import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from ai_agents.cli.quality import main
from ai_agents.validation.models import Dataset
from ai_agents.validation.quality import (
    QualityConfig,
    QualityTarget,
    check_quality_dataset,
    run_quality_with_client,
)
from ai_agents.validation.quality_reviews import (
    QualityRatings,
    checked_reviews,
    compare_quality,
    export_quality_report,
    quality_summary,
    review_template,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = QualityConfig(model="test-model", base_url="http://quality-test.invalid/v1")


def dataset(purpose):
    return Dataset.model_validate_json((ROOT / f"samples/{purpose}/quality-v1.json").read_text())


def mock_output(body):
    """전송·검증 파이프라인 시험용 결정 응답. 품질 정답으로 사용하지 않는다."""
    payload = json.loads(body["messages"][1]["content"])
    if "turns" in payload:
        user = payload["turns"][0]["messages"][0]
        criterion = payload["rubric"]["items"][0]["item_id"] if payload["rubric"] else None
        return {
            "items": [{"item_id": "I1", "criterion_id": criterion, "kind": "improvement",
                       "diagnosis": "테스트 전송 대역 응답", "evidence": [
                           {"message_id": user["message_id"], "quote": user["content"]},
                       ]}],
            "coaching": [{"item_id": "I1", "target_message_id": user["message_id"],
                          "next_action": "검증 방법을 기록하세요.",
                          "suggested_prompt": "[대상]에 관한 근거와 확인 방법을 제시해줘."}],
        }
    items = []
    for criterion, spec in payload["criterion_requirements"].items():
        can_score = spec["can_score"]
        text = next((row["text"] for row in payload["work_evidence_catalog"]
                     if row["id"] in spec["required_work_choice_ids"]), None)
        reason = f"「{text[:80]}」" if text else "[근거 없음]"
        items.append({
            "criterion_id": criterion, "observation": "observed" if can_score else "unobserved",
            "score": spec["allowed_scores"][0] if can_score else None,
            "reason": reason + " 테스트 전송 대역 응답", "review_required": False,
            "required_work_evidence_id": spec["required_work_choice_ids"][0] if can_score else None,
            "work_evidence_ids": [],
            "required_rule_evidence_id": spec["required_rule_choice_ids"][0],
            "rule_evidence_ids": [],
        })
    return {"items": items, "coaching": [
        {"criterion_id": row["criterion_id"], "action": "검증 방법을 기록하세요."} for row in items
    ]}


def transport(calls, *, bad_output=False, server_error=False, usage=True):
    def handle(request):
        assert request.url.host == "quality-test.invalid"
        assert request.url.path == "/v1/chat/completions"
        assert "x-work-id" not in request.headers and "x-user-id" not in request.headers
        calls.append(json.loads(request.content))
        if server_error:
            return httpx.Response(503, json={"error": {"message": "PRIVATE SERVER ERROR"}})
        result = {} if bad_output else mock_output(calls[-1])
        return httpx.Response(200, json={
            "id": "test-response", "object": "chat.completion", "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(result),
            }}],
            **({"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
               if usage else {}),
        })
    return httpx.MockTransport(handle)


async def run(tmp_path, purpose="feedback", *, count=1, **transport_options):
    data = dataset(purpose)
    data.examples = data.examples[:count]
    for case in data.examples:
        case.expected_description = "EXPECTED_TEXT_MUST_NOT_REACH_MODEL"
    calls = []
    async with AsyncOpenAI(
        base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
        http_client=httpx.AsyncClient(transport=transport(calls, **transport_options)),
    ) as client:
        result = await run_quality_with_client(
            data, client, CONFIG, output_dir=tmp_path / "experiment", repo=ROOT, label="test",
        )
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,count", [("feedback", 20), ("final", 8)])
async def test_all_cases_through_real_sdk_without_mcp(tmp_path, purpose, count, monkeypatch):
    async def prohibited(*args, **kwargs):
        raise AssertionError("MCP must not be called")

    monkeypatch.setattr("ai_agents.integrations.mcp_client.MCPGateway.call", prohibited)
    result, calls = await run(tmp_path, purpose, count=count)
    assert len(calls) == count and all(c.verdict == "passed" for c in result.cases)
    assert "EXPECTED_TEXT_MUST_NOT_REACH_MODEL" not in json.dumps(calls)
    assert "expected_description" not in json.dumps(calls)
    assert all(call["model"] == CONFIG.model and call["temperature"] == 0 for call in calls)
    summary = quality_summary(result)
    assert summary["execution_mode"] == "test_transport"
    assert summary["quality_mean"]["grounding"] is None and summary["reviewed"] == 0
    assert summary["tokens"] == {"input_tokens": count * 10, "output_tokens": count * 20}
    assert (tmp_path / "experiment" / "report.csv").exists()
    traces = "".join(p.read_text() for p in (tmp_path / "experiment/traces").glob("*.jsonl"))
    assert "test-secret" not in traces and "테스트 전송 대역 응답" not in traces
    assert result.manifest["langsmith_requested"] is False
    assert result.manifest["langsmith_export_failed"] is False
    if purpose == "final":
        for case in result.cases:
            if "missing-policy" in case.case_id:
                security = next(i for i in case.actual["output"]["result"]["items"]
                                if i["criterion_id"].endswith(".security"))
                assert security["score"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["feedback", "final"])
async def test_invalid_outputs_record_failed_generation_and_retry_count(tmp_path, purpose):
    result, calls = await run(tmp_path, purpose, bad_output=True)
    summary = quality_summary(result)
    assert summary["failed"] == 1 and summary["model_calls"] == len(calls) == 2
    assert summary["quality_mean"]["judgment"] is None
    assert result.cases[0].verdict == "failed"


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_not_zero(tmp_path):
    result, _ = await run(tmp_path, usage=False)
    assert quality_summary(result)["tokens"] == {"input_tokens": None, "output_tokens": None}


@pytest.mark.asyncio
async def test_api_failure_hides_error_text_and_cannot_be_human_scored(tmp_path):
    result, _ = await run(tmp_path, server_error=True)
    assert quality_summary(result)["model_calls"] == 1
    assert "PRIVATE SERVER ERROR" not in result.model_dump_json()
    sheet = review_template(result)
    mark_reviewed(sheet.cases[0])
    with pytest.raises(ValueError, match="failed generations"):
        checked_reviews(result, sheet)


def mark_reviewed(row, score=2, reviewer="reviewer-a"):
    row.status, row.reviewer = "reviewed", reviewer
    row.reviewed_at = datetime.now(timezone.utc)
    row.ratings = QualityRatings(grounding=score, judgment=score, actionability=score, scope=score)
    row.notes = "검사 코드 시험용 점수. 실제 모델 품질 판정이 아님."


@pytest.mark.asyncio
async def test_reviews_bound_to_exact_output_and_experiment(tmp_path):
    result, _ = await run(tmp_path, count=2)
    sheet = review_template(result)
    mark_reviewed(sheet.cases[0])
    summary = quality_summary(result, sheet)
    assert summary["reviewed"] == 1 and summary["review_coverage"] == 0.5
    assert summary["quality_mean"]["judgment"] == 2
    for field in ("experiment_id", "experiment_sha256", "dataset_sha256"):
        changed = sheet.model_copy(deep=True)
        setattr(changed, field, "wrong")
        with pytest.raises(ValueError):
            checked_reviews(result, changed)
    duplicate = sheet.model_copy(deep=True)
    duplicate.cases[1] = duplicate.cases[0]
    with pytest.raises(ValueError):
        checked_reviews(result, duplicate)
    sheet.cases[0].output_sha256 = "a" * 64
    with pytest.raises(ValueError, match="output changed"):
        checked_reviews(result, sheet)


@pytest.mark.asyncio
async def test_comparison_only_uses_paired_reviews_and_compatible_runs(tmp_path):
    before, _ = await run(tmp_path, count=2)
    after = before.model_copy(deep=True)
    after.manifest["experiment_id"] = "after"
    left, right = review_template(before), review_template(after)
    mark_reviewed(left.cases[0], 0)
    mark_reviewed(left.cases[1], 2)
    mark_reviewed(right.cases[0], 1, "reviewer-b")
    comparison = compare_quality(before, after, left, right)
    assert comparison["paired_reviewed_case_ids"] == [left.cases[0].case_id]
    assert comparison["paired_quality_mean"]["grounding"] == {"before": 0, "after": 1}
    assert comparison["different_reviewers"] == [left.cases[0].case_id]
    after.manifest["dataset_sha256"] = "changed"
    with pytest.raises(ValueError):
        compare_quality(before, after)
    after = before.model_copy(deep=True)
    after.manifest["model_settings"]["execution_mode"] = "real_model"
    with pytest.raises(ValueError, match="cannot be compared"):
        compare_quality(before, after, allow_changes=["model_settings"])


@pytest.mark.asyncio
async def test_report_and_cli_do_not_invent_human_scores_or_overwrite(tmp_path):
    result, _ = await run(tmp_path)
    report_dir = tmp_path / "report"
    export_quality_report(result, None, report_dir)
    text = (report_dir / "report.md").read_text()
    assert "미검토" in text and "실제 LLM 품질·지연 시간 증거가 아니다" in text
    source = tmp_path / "experiment/experiment.json"
    sheet = tmp_path / "reviews.json"
    assert main(["review-template", str(source), "--output", str(sheet)]) == 0
    assert main(["review-template", str(source), "--output", str(sheet)]) == 2
    assert main(["report", str(source), "--reviews", str(sheet),
                 "--output-dir", str(report_dir)]) == 2


def test_snapshot_whitespace_survives_dataset_roundtrip():
    data = dataset("final")
    raw = json.loads((ROOT / "samples/final/quality-v1.json").read_text())
    assert data.examples[0].inputs == raw["examples"][0]["inputs"]
    copy = Dataset.model_validate_json(data.model_dump_json())
    assert check_quality_dataset(copy)["cases"] == 8
    value = deepcopy(raw)
    value["examples"][0]["inputs"]["model_output"] = {"leaked": "answer"}
    with pytest.raises(ValidationError):
        check_quality_dataset(Dataset.model_validate(value))


@pytest.mark.parametrize("url", ["", "ftp://test", "https://user:password@test/v1",
                                 "https://test/v1?api_key=secret", "https://test/v1#token"])
def test_endpoint_must_be_explicit_and_credentials_separate(url):
    with pytest.raises(ValidationError):
        QualityConfig(model="model", base_url=url)


@pytest.mark.asyncio
async def test_langsmith_export_failure_is_recorded_without_raw_content(tmp_path, monkeypatch):
    events = []

    class Sink:
        def start(self, event):
            events.append(event)

        def finish(self, event):
            events.append(event)

        def close(self):
            raise RuntimeError("PRIVATE TELEMETRY ERROR")

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-secret")
    monkeypatch.setattr("ai_agents.observability.tracing.LangSmithSink", lambda project: Sink())
    data = dataset("feedback")
    data.examples = data.examples[:1]
    async with AsyncOpenAI(
        base_url=CONFIG.base_url, api_key="test-secret", max_retries=0,
        http_client=httpx.AsyncClient(transport=transport([])),
    ) as client:
        with pytest.warns(RuntimeWarning, match="Trace export failed"):
            result = await run_quality_with_client(
                data, client, CONFIG, output_dir=tmp_path / "experiment", repo=ROOT,
                label="export-test", langsmith=True,
            )
    assert result.manifest["langsmith_export_failed"] is True
    assert result.manifest["langsmith_requested"] is True
    assert "테스트 전송 대역 응답" not in json.dumps(events, ensure_ascii=False)
    assert "test-secret" not in json.dumps(events)


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=asyncio.CancelledError()),
    )))
    with pytest.raises(asyncio.CancelledError):
        await QualityTarget("feedback", client, CONFIG, "test_transport")(
            dataset("feedback").examples[0].inputs,
        )
