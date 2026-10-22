"""exchange_id 기준 질문·답변 1쌍, 요청 범위와 인용 근거를 검증한다."""

import re
from collections import defaultdict

from ...shared.live_feedback_contracts import (
    GeneratedFeedback,
    LiveEvaluationContext,
    LiveEvaluationEvent,
    MessageBatch,
)
from .errors import FeedbackError

# 전체 개인정보 탐지기가 아니다. MCP의 실제 마스킹을 필수로 요구하고 잔여 패턴도 거른다.
SENSITIVE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)|"
    r"\b(?:Bearer\s+[A-Za-z0-9_.-]{8,}|sk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


def validate_context(event: LiveEvaluationEvent, context: LiveEvaluationContext) -> None:
    for key in (
        "live_eval_id",
        "company_id",
        "work_id",
        "requested_by",
        "from_seq",
        "to_seq",
        "rubric_version",
    ):
        if getattr(event, key) != getattr(context, key):
            raise FeedbackError("SCOPE_MISMATCH")
    if context.status == "대기":
        raise FeedbackError("CONTRACT_MISMATCH")


def validate_messages(context: LiveEvaluationContext, batch: MessageBatch) -> list[dict]:
    for key in ("live_eval_id", "work_id", "from_seq", "to_seq"):
        if getattr(context, key) != getattr(batch, key):
            raise FeedbackError("SCOPE_MISMATCH")
    if not batch.complete:
        raise FeedbackError("CONTRACT_MISMATCH")
    if not batch.masking_applied:
        raise FeedbackError("UNMASKED_INPUT")
    if not batch.messages:
        raise FeedbackError("EMPTY_CONVERSATION")
    rows = sorted(batch.messages, key=lambda m: m.seq)
    if len({m.seq for m in rows}) != len(rows) or len({m.message_id for m in rows}) != len(rows):
        raise FeedbackError("INVALID_SEQUENCE")
    if rows[0].seq != context.from_seq or rows[-1].seq != context.to_seq:
        raise FeedbackError("INVALID_SEQUENCE")
    if any(
        not context.from_seq <= m.seq <= context.to_seq or m.created_at > context.requested_at
        for m in rows
    ):
        raise FeedbackError("SCOPE_MISMATCH")
    if sum(len(m.content) for m in rows) > 50000:
        raise FeedbackError("INPUT_TOO_LARGE")
    if any(SENSITIVE.search(m.content) for m in rows):
        raise FeedbackError("UNMASKED_INPUT")
    groups = defaultdict(list)
    for row in rows:
        groups[row.exchange_id].append(row)
    if not 1 <= len(groups) <= 5:
        raise FeedbackError("INCOMPLETE_EXCHANGE")
    turns = []
    previous_end = 0
    for exchange_id, pair in groups.items():
        if len(pair) != 2 or [m.role for m in pair] != ["user", "assistant"]:
            raise FeedbackError("INCOMPLETE_EXCHANGE")
        if pair[0].seq <= previous_end:
            raise FeedbackError("INVALID_SEQUENCE")
        previous_end = pair[1].seq
        turns.append(
            {"exchange_id": exchange_id, "messages": [m.model_dump(mode="json") for m in pair]}
        )
    return turns


def validate_feedback(
    result: GeneratedFeedback, context: LiveEvaluationContext, batch: MessageBatch
) -> None:
    sources = {m.message_id: m for m in batch.messages}
    criteria = {i.item_id for i in context.rubric.items} if context.rubric else set()
    items = {i.item_id: i for i in result.items}
    if len(items) != len(result.items):
        raise FeedbackError("INVALID_FEEDBACK")
    for item in result.items:
        if item.criterion_id is not None and item.criterion_id not in criteria:
            raise FeedbackError("INVALID_FEEDBACK")
        for evidence in item.evidence:
            if (
                evidence.message_id not in sources
                or evidence.quote not in sources[evidence.message_id].content
            ):
                raise FeedbackError("INVALID_FEEDBACK")
    coached = set()
    for coaching in result.coaching:
        if (
            coaching.item_id not in items
            or coaching.item_id in coached
            or coaching.target_message_id not in sources
            or sources[coaching.target_message_id].role != "user"
        ):
            raise FeedbackError("INVALID_FEEDBACK")
        coached.add(coaching.item_id)
    if any(item.kind == "improvement" and item.item_id not in coached for item in result.items):
        raise FeedbackError("INVALID_FEEDBACK")
    if SENSITIVE.search(result.model_dump_json()):
        raise FeedbackError("SENSITIVE_OUTPUT")
