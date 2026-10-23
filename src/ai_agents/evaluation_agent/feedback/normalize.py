"""모델 출력의 근거 연결만 바로잡는다.

내용은 만들거나 고치지 않으며, 결과는 validate_feedback이 다시 검사한다.

모델(특히 OpenAI 소형 모델)이 자주 틀리는 것:
- 인용을 다른 메시지 ID에 붙이거나, 마크다운 강조 기호·공백을 빼고 인용한다
- 코칭 대상을 사용자 질문이 아니라 같은 회차의 AI 답변으로 고른다
"""

from collections import defaultdict

from ...shared.live_feedback_contracts import FeedbackEvidence, GeneratedFeedback, MessageBatch

MARKUP = "*_`"
QUOTE_EDGES = " \t\r\n\"'“”‘’「」『』" + MARKUP
MIN_LOOSE_QUOTE = 4  # 마크다운·공백을 무시하고 찾을 때 최소 글자 수(너무 짧으면 엉뚱한 곳에 붙는다)


def normalize_feedback(
    result: GeneratedFeedback, batch: MessageBatch
) -> tuple[GeneratedFeedback, dict]:
    """바로잡은 결과와 바로잡은 횟수(원문 없음)를 돌려준다.

    남는 항목·코칭이 없으면 ValidationError.
    """
    messages = {m.message_id: m for m in batch.messages}
    turn = defaultdict(dict)
    for m in batch.messages:
        turn[m.exchange_id][m.role] = m.message_id
    question_of = {
        t["assistant"]: t["user"] for t in turn.values() if "user" in t and "assistant" in t
    }
    stats = {
        "evidence_moved": 0,
        "evidence_dropped": 0,
        "items_dropped": 0,
        "targets_moved": 0,
        "coaching_dropped": 0,
    }

    items = []
    for item in result.items:
        evidence = []
        for ev in item.evidence:
            fixed = _locate(ev, messages, turn)
            if fixed is None:
                stats["evidence_dropped"] += 1
                continue
            if fixed != ev:
                stats["evidence_moved"] += 1
            if fixed not in evidence:
                evidence.append(fixed)
        if evidence:
            items.append(item.model_copy(update={"evidence": evidence}))
        else:
            stats["items_dropped"] += 1

    kept, coached, coaching = {i.item_id for i in items}, set(), []
    for c in result.coaching:
        if c.item_id not in kept or c.item_id in coached:
            stats["coaching_dropped"] += 1
            continue
        target = c.target_message_id
        if target in messages and messages[target].role == "assistant" and target in question_of:
            target = question_of[target]
            stats["targets_moved"] += 1
        coaching.append(c.model_copy(update={"target_message_id": target}))
        coached.add(c.item_id)

    fixed = GeneratedFeedback.model_validate(
        {"items": [i.model_dump() for i in items], "coaching": [c.model_dump() for c in coaching]}
    )
    return fixed, stats


def _locate(ev: FeedbackEvidence, messages: dict, turn: dict) -> FeedbackEvidence | None:
    """인용이 글자 그대로 있는 메시지를 찾는다.

    가리킨 메시지 → 같은 회차 → 대화 전체에서 한 곳뿐일 때 순서로 본다.
    """
    cited = messages.get(ev.message_id)
    if cited is not None and (span := exact_span(ev.quote, cited.content)):
        return FeedbackEvidence(message_id=cited.message_id, quote=span)
    if cited is not None:
        for message_id in turn[cited.exchange_id].values():
            if message_id != cited.message_id and (
                span := exact_span(ev.quote, messages[message_id].content)
            ):
                return FeedbackEvidence(message_id=message_id, quote=span)
    hits = [
        (m.message_id, span) for m in messages.values() if (span := exact_span(ev.quote, m.content))
    ]
    if len(hits) == 1:
        return FeedbackEvidence(message_id=hits[0][0], quote=hits[0][1])
    return None


def exact_span(quote: str, content: str) -> str | None:
    """quote가 content에 그대로 있으면 그대로 돌려준다.

    마크다운 강조·따옴표·공백만 다르면 content의 해당 원문 구간을 돌려준다.
    """
    if quote and quote in content:
        return quote
    needle, _ = _plain(quote.strip(QUOTE_EDGES))
    needle = needle.strip()
    if len(needle) < MIN_LOOSE_QUOTE:
        return None
    hay, index = _plain(content)
    at = hay.find(needle)
    if at < 0:
        return None
    return content[index[at] : index[at + len(needle) - 1] + 1]


def _plain(text: str) -> tuple[str, list[int]]:
    """마크다운 강조 기호를 빼고 공백을 하나로 줄인 글자와, 각 글자의 원래 위치."""
    chars, index, space = [], [], False
    for i, ch in enumerate(text):
        if ch in MARKUP:
            continue
        if ch.isspace():
            if space:
                continue
            ch, space = " ", True
        else:
            space = False
        chars.append(ch)
        index.append(i)
    return "".join(chars), index
