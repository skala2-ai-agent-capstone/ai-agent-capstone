import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ai_agents.evaluation_agent.feedback.agent import FeedbackAgent
from ai_agents.evaluation_agent.feedback.errors import FeedbackError
from ai_agents.evaluation_agent.feedback.validation import validate_feedback, validate_messages
from ai_agents.orchestration.feedback_worker import DLQ, RETRY_QUEUE, FeedbackWorker
from ai_agents.orchestration.live_feedback import LiveFeedbackWorkflow
from ai_agents.shared.live_feedback_contracts import (
    GeneratedFeedback,
    LiveEvaluationContext,
    LiveEvaluationEvent,
    MessageBatch,
)
from ai_agents.validation.feedback_targets import FixtureChatClient, FixtureGateway, replay_feedback

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture():
    return json.loads((ROOT / "samples/feedback/complete-five.json").read_text())


def typed(data):
    return (
        LiveEvaluationContext.model_validate(data["context"]),
        MessageBatch.model_validate(data["batch"]),
    )


def test_five_exchanges_are_ten_messages_and_sorted(fixture):
    fixture["batch"]["messages"].reverse()
    turns = validate_messages(*typed(fixture))
    assert len(turns) == 5
    assert [t["exchange_id"] for t in turns] == [101, 102, 103, 104, 105]
    assert all([m["role"] for m in t["messages"]] == ["user", "assistant"] for t in turns)


@pytest.mark.parametrize(
    "change,code",
    [
        ("masking", "UNMASKED_INPUT"),
        ("phone", "UNMASKED_INPUT"),
        ("future", "SCOPE_MISMATCH"),
        ("outside", "INVALID_SEQUENCE"),
        ("duplicate_seq", "INVALID_SEQUENCE"),
        ("missing_answer", "INCOMPLETE_EXCHANGE"),
        ("interleaved", "INVALID_SEQUENCE"),
        ("partial", "CONTRACT_MISMATCH"),
    ],
)
def test_invalid_message_windows(fixture, change, code):
    rows = fixture["batch"]["messages"]
    if change == "masking":
        fixture["batch"]["masking_applied"] = False
    elif change == "phone":
        rows[0]["content"] = "010-1234-5678로 연락해주세요."
    elif change == "future":
        rows[-1]["created_at"] = "2026-10-23T00:00:00+09:00"
    elif change == "outside":
        rows[-1]["seq"] += 1
    elif change == "duplicate_seq":
        rows[1]["seq"] = rows[0]["seq"]
    elif change == "missing_answer":
        del rows[3]
    elif change == "interleaved":
        rows[1]["seq"], rows[2]["seq"] = rows[2]["seq"], rows[1]["seq"]
    elif change == "partial":
        fixture["batch"]["complete"] = False
    with pytest.raises(FeedbackError, match=code):
        validate_messages(*typed(fixture))


def test_rejects_five_message_interpretation_and_accepts_one_pair(fixture):
    fixture["batch"]["messages"] = fixture["batch"]["messages"][:5]
    fixture["batch"]["to_seq"] = fixture["context"]["to_seq"] = 5
    with pytest.raises(FeedbackError, match="INCOMPLETE_EXCHANGE"):
        validate_messages(*typed(fixture))
    fixture["batch"]["messages"] = fixture["batch"]["messages"][:2]
    fixture["batch"]["to_seq"] = fixture["context"]["to_seq"] = 2
    assert len(validate_messages(*typed(fixture))) == 1


@pytest.mark.parametrize(
    "change",
    [
        "unknown_id",
        "fake_quote",
        "criterion",
        "assistant_target",
        "duplicate_item",
        "no_action",
        "secret",
    ],
)
def test_generated_output_is_grounded(fixture, change):
    output = fixture["model_output"]
    if change == "unknown_id":
        output["items"][0]["evidence"][0]["message_id"] = "UNKNOWN"
    elif change == "fake_quote":
        output["items"][0]["evidence"][0]["quote"] = "이 문장은 원문에 없습니다."
    elif change == "criterion":
        output["items"][0]["criterion_id"] = "invented-criterion"
    elif change == "assistant_target":
        output["coaching"][0]["target_message_id"] = "W-DEMO-M10"
    elif change == "duplicate_item":
        output["items"].append(deepcopy(output["items"][0]))
    elif change == "no_action":
        output["items"][0]["item_id"] = "other-item"
    elif change == "secret":
        output["coaching"][0]["suggested_prompt"] = "kim@example.com으로 보내세요."
    with pytest.raises(FeedbackError):
        validate_feedback(GeneratedFeedback.model_validate(output), *typed(fixture))


def test_queue_rejects_raw_content_and_inverted_range(fixture):
    raw = fixture["event"]
    with pytest.raises(ValidationError):
        LiveEvaluationEvent.model_validate({**raw, "content": "private"})
    with pytest.raises(ValidationError):
        LiveEvaluationEvent.model_validate({**raw, "from_seq": raw["to_seq"] + 1})
    with pytest.raises(ValidationError):
        LiveEvaluationEvent.model_validate({**raw, "attempt": True})


@pytest.mark.asyncio
async def test_complete_workflow_stores_provenance_and_bound_range(fixture):
    gateway, client = FixtureGateway(fixture), FixtureChatClient(fixture["model_output"])
    result = await LiveFeedbackWorkflow(gateway, FeedbackAgent(client, "test-model")).run(
        LiveEvaluationEvent.model_validate(fixture["event"]),
        trace_id="trace-test",
        code_provenance={"git_commit": "abc", "dirty": True},
    )
    assert result["status"] == "완료"
    assert gateway.calls == ["start_live_evaluation", "get_messages", "save_live_evaluation"]
    assert len(client.calls) == 1
    assert client.calls[0]["extra_headers"] == {
        "X-Stage": "live_evaluate",
        "X-Work-Id": fixture["context"]["work_id"],
        "X-User-Id": fixture["context"]["requested_by"],
    }
    assert len(json.loads(client.calls[0]["messages"][1]["content"])["turns"]) == 5
    provenance = gateway.saved["coaching"]["provenance"]
    assert provenance["exchange_ids"] == [101, 102, 103, 104, 105]
    assert provenance["mode"] == "general_prompt_coaching"
    assert len(provenance["source_sha256"]) == 64
    assert provenance["code"]["git_commit"] == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["완료", "실패"])
async def test_terminal_delivery_skips_llm_and_save(fixture, status):
    fixture["context"]["status"] = status
    result = await replay_feedback(fixture)
    assert result["status"] == status and result["duplicate"]
    assert result["model_calls"] == 0 and not result["saved"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("company_id", "OTHER"), ("requested_by", "OTHER"), ("to_seq", 20), ("rubric_version", "v9")],
)
async def test_queue_cannot_override_canonical_scope(fixture, field, value):
    fixture["event"][field] = value
    result = await replay_feedback(fixture)
    assert result["code"] == "SCOPE_MISMATCH"
    assert result["model_calls"] == 0


@pytest.mark.asyncio
async def test_bad_generation_retries_once_and_never_saves(fixture):
    fixture["model_output"]["items"][0]["evidence"][0]["quote"] = "invented"
    result = await replay_feedback(fixture)
    assert result["code"] == "INVALID_FEEDBACK"
    assert result["model_calls"] == 2 and not result["saved"]


@pytest.mark.asyncio
async def test_proxy_headers_stay_scoped_across_requests_and_regeneration(fixture):
    client = FixtureChatClient(fixture["model_output"])
    agent = FeedbackAgent(client, "test-model")
    await agent.generate(*typed(fixture))
    fixture["context"]["requested_by"] = "U-OTHER"
    fixture["model_output"]["items"][0]["evidence"][0]["quote"] = "invented"
    with pytest.raises(FeedbackError, match="INVALID_FEEDBACK"):
        await agent.generate(*typed(fixture))
    assert len(client.calls) == 3
    assert client.calls[0]["extra_headers"]["X-User-Id"] == "U-DEMO"
    assert client.calls[1]["extra_headers"] == client.calls[2]["extra_headers"] == {
        "X-Stage": "live_evaluate",
        "X-Work-Id": fixture["context"]["work_id"],
        "X-User-Id": "U-OTHER",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["work_id", "requested_by"])
async def test_rejects_ids_that_proxy_would_truncate_before_model_call(fixture, field):
    fixture["context"][field] = "X" * 129
    if field == "work_id":
        fixture["batch"][field] = fixture["context"][field]
    client = FixtureChatClient(fixture["model_output"])
    with pytest.raises(FeedbackError, match="CONTRACT_MISMATCH"):
        await FeedbackAgent(client, "test-model").generate(*typed(fixture))
    assert not client.calls


@pytest.mark.asyncio
async def test_save_retry_does_not_regenerate(fixture):
    gateway, client = FixtureGateway(fixture), FixtureChatClient(fixture["model_output"])
    call = gateway.call
    attempts = []

    async def flaky(tool, args):
        if tool == "save_live_evaluation":
            attempts.append(deepcopy(args))
            if len(attempts) == 1:
                raise FeedbackError("MCP_UNAVAILABLE", retryable=True)
        return await call(tool, args)

    gateway.call = flaky
    await LiveFeedbackWorkflow(
        gateway, FeedbackAgent(client, "fixture-model"), sleep=AsyncMock()
    ).run(
        LiveEvaluationEvent.model_validate(fixture["event"]),
        trace_id="trace",
        code_provenance={},
    )
    assert len(client.calls) == 1
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt,transient,queue", [(0, True, RETRY_QUEUE), (3, True, DLQ), (0, False, DLQ)]
)
async def test_retry_and_dlq_are_published_before_ack(fixture, tmp_path, attempt, transient, queue):
    order = []
    fixture["event"]["attempt"] = attempt

    async def execute(*args):
        raise FeedbackError("MCP_UNAVAILABLE", retryable=transient)

    async def publish(destination, payload, code):
        assert destination == queue
        assert payload["attempt"] == (attempt + 1 if queue == RETRY_QUEUE else attempt)
        order.append("publish-confirmed")

    async def ack():
        order.append("ack")

    worker = FeedbackWorker(execute, trace_dir=tmp_path, model="test", code_provenance={})
    await worker.handle(json.dumps(fixture["event"]).encode(), publish=publish, ack=ack)
    assert order == ["publish-confirmed", "ack"]
    trace = next(tmp_path.glob("trace-*.jsonl")).read_text()
    assert '"error_code": "MCP_UNAVAILABLE"' in trace


@pytest.mark.asyncio
async def test_publish_failure_does_not_ack_and_malformed_body_is_not_forwarded(tmp_path):
    worker = FeedbackWorker(AsyncMock(), trace_dir=tmp_path, model="test", code_provenance={})
    ack, publish = AsyncMock(), AsyncMock(side_effect=RuntimeError("broker unavailable"))
    with pytest.raises(RuntimeError):
        await worker.handle(b'{"secret": "private"}', publish=publish, ack=ack)
    ack.assert_not_awaited()
    assert "private" not in str(publish.call_args)
    worker.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_trace_does_not_contain_conversation(fixture, tmp_path):
    gateway, client = FixtureGateway(fixture), FixtureChatClient(fixture["model_output"])

    async def execute(event, trace, code):
        return await LiveFeedbackWorkflow(gateway, FeedbackAgent(client, "test")).run(
            event,
            trace_id=trace,
            code_provenance=code,
        )

    ack, publish = AsyncMock(), AsyncMock()
    worker = FeedbackWorker(execute, trace_dir=tmp_path, model="test", code_provenance={})
    result = await worker.handle(json.dumps(fixture["event"]).encode(), publish=publish, ack=ack)
    assert result["status"] == "완료" and gateway.saved
    ack.assert_awaited_once()
    publish.assert_not_awaited()
    trace = next(tmp_path.glob("trace-*.jsonl")).read_text()
    assert fixture["batch"]["messages"][0]["content"] not in trace
    assert '"status": "완료"' in trace


@pytest.mark.asyncio
async def test_generated_feedback_with_pinned_criteria(fixture):
    fixture["event"]["rubric_version"] = "v2"
    fixture["context"].update(
        rubric_doc_id="RUBRIC-DEMO",
        rubric_version="v2",
        rubric={
            "doc_id": "RUBRIC-DEMO",
            "version": "v2",
            "items": [
                {
                    "item_id": "instruction_quality",
                    "item": "지시 구체성",
                    "criterion": "요청에 대상·목적·출력 형식을 명시한다.",
                }
            ],
        },
    )
    fixture["model_output"]["items"][0]["criterion_id"] = "instruction_quality"
    result = await replay_feedback(fixture)
    assert result["status"] == "완료"
    fixture["context"]["rubric"] = None
    assert (await replay_feedback(fixture))["code"] == "CONTRACT_MISMATCH"
