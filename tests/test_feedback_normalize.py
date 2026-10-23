"""모델 출력의 근거 연결 보정(normalize_feedback)과 재시도 안내."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_agents.evaluation_agent.feedback.agent import FeedbackAgent
from ai_agents.evaluation_agent.feedback.errors import FeedbackError
from ai_agents.evaluation_agent.feedback.normalize import exact_span, normalize_feedback
from ai_agents.evaluation_agent.feedback.validation import validate_feedback
from ai_agents.shared.live_feedback_contracts import (
    GeneratedFeedback,
    LiveEvaluationContext,
    MessageBatch,
)
from ai_agents.validation.feedback_targets import FixtureChatClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture():
    return json.loads((ROOT / "samples/feedback/complete-five.json").read_text())


def typed(data):
    return (
        LiveEvaluationContext.model_validate(data["context"]),
        MessageBatch.model_validate(data["batch"]),
    )


def content(data, message_id):
    return next(m["content"] for m in data["batch"]["messages"] if m["message_id"] == message_id)


def normalized(data):
    context, batch = typed(data)
    result, stats = normalize_feedback(
        GeneratedFeedback.model_validate(data["model_output"]), batch
    )
    validate_feedback(result, context, batch)
    return result, stats


def test_assistant_target_moves_to_question_of_same_turn(fixture):
    fixture["model_output"]["coaching"][0]["target_message_id"] = "W-DEMO-M10"  # M09의 답변

    result, stats = normalized(fixture)

    assert result.coaching[0].target_message_id == "W-DEMO-M09"
    assert stats["targets_moved"] == 1


def test_quote_on_wrong_message_moves_to_message_that_has_it(fixture):
    quote = content(fixture, "W-DEMO-M07")[:12]
    fixture["model_output"]["items"][0]["evidence"][0]["quote"] = quote  # M09에 붙였지만 M07의 문장

    result, stats = normalized(fixture)

    assert result.items[0].evidence[0].message_id == "W-DEMO-M07"
    assert result.items[0].evidence[0].quote == quote
    assert stats["evidence_moved"] == 1


def test_loose_quote_maps_to_exact_source_span():
    source = "현재 **'제품 설명서'** 파일이\n  없습니다."
    assert exact_span("현재 '제품 설명서' 파일이 없습니다.", source) == source
    assert exact_span('"제품 설명서"', "이 제품 설명서 기준으로") == "제품 설명서"
    assert exact_span("없는 문장입니다", source) is None
    assert exact_span("**파일**", "파일 없음") is None  # 마크다운을 걷어내면 너무 짧다


def test_unverifiable_evidence_drops_only_that_item(fixture):
    output = fixture["model_output"]
    extra = deepcopy(output["items"][0])
    extra.update(item_id="feedback-2", kind="strength")
    extra["evidence"][0]["quote"] = "대화 어디에도 없는 문장입니다"
    output["items"].append(extra)

    result, stats = normalized(fixture)

    assert [i.item_id for i in result.items] == ["feedback-1"]
    assert stats["items_dropped"] == 1 and stats["evidence_dropped"] == 1


def test_nothing_grounded_is_still_rejected(fixture):
    fixture["model_output"]["items"][0]["evidence"][0]["quote"] = "invented"
    with pytest.raises(ValidationError):
        normalize_feedback(
            GeneratedFeedback.model_validate(fixture["model_output"]), typed(fixture)[1]
        )


def test_second_coaching_for_same_item_is_dropped(fixture):
    output = fixture["model_output"]
    output["coaching"].append(deepcopy(output["coaching"][0]))

    result, stats = normalized(fixture)

    assert len(result.coaching) == 1 and stats["coaching_dropped"] == 1


@pytest.mark.asyncio
async def test_agent_fixes_assistant_target_without_regenerating(fixture):
    fixture["model_output"]["coaching"][0]["target_message_id"] = "W-DEMO-M10"
    client = FixtureChatClient(fixture["model_output"])

    result = await FeedbackAgent(client, "test-model").generate(*typed(fixture))

    assert result.coaching[0].target_message_id == "W-DEMO-M09"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_retry_hint_lists_user_ids_without_conversation_text(fixture):
    fixture["model_output"]["items"][0]["evidence"][0]["quote"] = "invented"
    client = FixtureChatClient(fixture["model_output"])

    with pytest.raises(FeedbackError, match="INVALID_FEEDBACK"):
        await FeedbackAgent(client, "test-model").generate(*typed(fixture))

    hint = client.calls[1]["messages"][-1]["content"]
    user_ids = [m["message_id"] for m in fixture["batch"]["messages"] if m["role"] == "user"]
    assert all(message_id in hint for message_id in user_ids)
    assert "W-DEMO-M10" not in hint  # AI 답변 ID는 코칭 대상으로 안내하지 않는다
    assert all(m["content"][:20] not in hint for m in fixture["batch"]["messages"])
