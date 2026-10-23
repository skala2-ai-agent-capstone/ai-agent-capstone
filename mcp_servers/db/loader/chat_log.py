"""업무 공간 대화(케이스의 messages)를 기록 프록시가 남기는 llm_exchanges 행으로 바꾼다.

업무 DB의 messages는 llm_exchanges를 펼쳐 보여주는 뷰이므로, 시드 대화도 이 모양으로 적재해야 한다.
적재기(load_postgres)와 모의 데이터 생성기(llm_proxy/mock/build_mock.py)가 같은 규칙을 쓰도록 여기에 둔다.

- 사용자 메시지 1개 = workspace_chat 호출 1건. 업무 공간은 답변을 스트리밍으로 보여준다.
- 바로 뒤에 답변이 있으면 성공한 호출(답변 = SSE 원문), 없으면(다음도 사용자 메시지이거나 마지막 메시지)
  답을 받기 전에 끊긴 호출(client_disconnected)로 남긴다. 뷰는 끊긴 호출의 질문만 보여준다.
- 시각: created_at = 답변 시각(끊긴 호출은 요청 직후), latency_ms = 질문부터 그때까지의 간격.
  뷰는 created_at - latency_ms를 질문 시각으로 쓰므로 원천 데이터의 메시지 시각이 그대로 나온다.
  그래서 시드 대화의 latency_ms는 실제 처리 시간이 아니라 사람이 다음 메시지를 보낸 간격이다.
- 토큰 수는 글자 수로 추정한 값이다(실측 아님). 같은 입력이면 항상 같은 값이 나온다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

MODEL = "gemma4"
WORKSPACE_MEMBER = "workspace-backend"
WORKSPACE_PROMPT = (
    "당신은 영업 담당자의 업무를 돕는 AI 어시스턴트다. 사용자가 첨부하거나 붙여 넣은 자료와 "
    "사내 제품·가격 자료만 근거로 답하고, 근거가 없으면 확인이 필요하다고 말한다. "
    "표를 요청받으면 마크다운 표로 작성한다."
)


def tokens(text: str) -> int:
    """한국어가 섞인 글의 대략적인 토큰 수(글자 1.4개당 1토큰)."""
    return max(1, round(len(text) / 1.4))


def short_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def generation_ms(key: str, input_tokens: int, output_tokens: int) -> int:
    """L4 + Gemma 4 26B-A4B 4bit 가정(입력 약 6,600 tok/s, 생성 약 45 tok/s). key로 고정된 ±10~15% 변동."""
    jitter = 0.9 + int(short_id("jitter", key)[:4], 16) / 0xFFFF * 0.25
    return int((250 + input_tokens * 0.15 + output_tokens * 22) * jitter)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def gap_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def sse(key: str, content: str, prompt: int, completion: int, at: datetime, *, cut: int | None = None) -> str:
    """Chat Completions 스트리밍 원문. cut이 있으면 그 글자에서 끊긴다(종료·usage·[DONE] 없음)."""
    def event(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    base = {"id": f"chatcmpl-mock-{key}", "object": "chat.completion.chunk", "created": int(at.timestamp()), "model": MODEL}
    text = content if cut is None else content[:cut]
    size = max(1, len(content) // 4)
    out = event({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    for i in range(0, len(text), size):
        out += event({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + size]}}]})
    if cut is not None:
        return out
    out += event({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    out += event({**base, "choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}})
    return out + "data: [DONE]\n\n"


def request_body(history: list[dict]) -> dict:
    return {"model": MODEL, "messages": [{"role": "system", "content": WORKSPACE_PROMPT}, *history],
            "temperature": 0.3, "max_tokens": 4096, "stream": True, "stream_options": {"include_usage": True}}


def workspace_exchanges(case: dict) -> list[dict]:
    """케이스 1건의 대화를 시간순 llm_exchanges 행 목록으로 바꾼다(id 없음)."""
    work = case["raw"]["work_case"]
    messages = sorted(case["raw"]["messages"], key=lambda m: m["seq"])
    history: list[dict] = []
    rows: list[dict] = []
    common = {"member": WORKSPACE_MEMBER, "endpoint": "/v1/chat/completions", "stage": "workspace_chat",
              "work_id": work["work_id"], "user_id": work["employee_id"], "model": MODEL, "stream": True,
              "status_code": 200, "response": None}
    for i, message in enumerate(messages):
        if message["role"] == "user":
            history.append({"role": "user", "content": message["content"]})
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            if nxt is not None and nxt["role"] == "assistant":
                continue  # 다음 답변 차례에 성공 호출로 남긴다
            # 답을 받기 전에 끊긴 호출: 질문만 남는다
            asked_at = datetime.fromisoformat(message["created_at"])
            ended_at = asked_at + timedelta(seconds=2)
            if nxt is not None:
                ended_at = min(ended_at, datetime.fromisoformat(nxt["created_at"]) - timedelta(milliseconds=1))
            key = short_id(work["work_id"], message["message_id"], "cut")
            rows.append({**common, "created_at": iso(ended_at), "error": "client_disconnected",
                         "latency_ms": gap_ms(asked_at, ended_at), "input_tokens": None, "output_tokens": None,
                         "request": request_body(list(history)), "response_text": sse(key, "", 0, 0, asked_at, cut=0)})
            continue
        # 답변: 바로 앞 사용자 메시지에 대한 성공 호출
        asked = next(m for m in reversed(messages[:i]) if m["role"] == "user")
        request = request_body(list(history))
        prompt = sum(tokens(m["content"]) for m in request["messages"])
        completion = tokens(message["content"])
        answered_at = datetime.fromisoformat(message["created_at"])
        key = short_id(work["work_id"], message["message_id"])
        rows.append({**common, "created_at": iso(answered_at), "error": None,
                     "latency_ms": gap_ms(datetime.fromisoformat(asked["created_at"]), answered_at),
                     "input_tokens": prompt, "output_tokens": completion, "request": request,
                     "response_text": sse(key, message["content"], prompt, completion, answered_at)})
        history.append({"role": "assistant", "content": message["content"]})
    return rows
