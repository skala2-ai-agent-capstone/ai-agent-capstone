"""LLM 호출 기록용 요청 헤더. 기록 프록시(llm_proxy)만 읽고 모델에는 전달되지 않는다.

X-Stage는 호출마다 붙인다. 프록시는 이 값으로 기록 단계를 남기고 OPENAI_STAGES 분기도 정한다.
X-Work-Id는 업무 DB에서 받은 업무를 처리하는 동안(work_scope 안)만 붙인다. 프록시가 DB에 없는
업무 ID를 400으로 거절하므로 샘플·가상 업무 실행은 X-Stage만 보낸다.
work_id는 분류 힌트가 되지 않도록 프롬프트에서 빼므로(WorkContext.llm_payload) 헤더로만 보낸다.
호출부 시그니처를 바꾸지 않도록 업무 범위는 contextvar로 넘긴다.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 프록시의 header_tag가 128자로 자른다. 잘린 ID는 업무 DB에서 찾을 수 없다.
MAX_WORK_ID = 128

_work_id: ContextVar[str | None] = ContextVar("llm_trace_work_id", default=None)


@contextmanager
def work_scope(work_id: str | None) -> Iterator[None]:
    if work_id is not None and len(work_id) > MAX_WORK_ID:
        raise ValueError("프록시 기록용 업무 ID 길이를 초과했습니다.")
    token = _work_id.set(work_id)
    try:
        yield
    finally:
        _work_id.reset(token)


def trace_headers(stage: str) -> dict[str, str]:
    headers = {"X-Stage": stage}
    if work_id := _work_id.get():
        headers["X-Work-Id"] = work_id
    return headers
