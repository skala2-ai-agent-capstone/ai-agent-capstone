"""비동기 작업 그룹에 감싸진 오류를 입력 본문 없이 표시한다."""

import re

from pydantic import ValidationError

from ...classification.agent import ClassificationError
from ...criteria_agent.builder import RubricError
from ...integrations.mcp_client import MCPError
from ...orchestration.approval import WorkflowError
from .validation import EvaluationError

KNOWN_ERRORS = (MCPError, ClassificationError, RubricError, EvaluationError, WorkflowError)
SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]+|\bBearer\s+[^\s,;]+", re.IGNORECASE)


def error_messages(error: BaseException) -> list[str]:
    """중첩 그룹을 펼치되 알 수 없는 오류·검증 오류의 원문은 출력하지 않는다."""
    if isinstance(error, BaseExceptionGroup):
        messages = []
        for child in error.exceptions:
            messages.extend(error_messages(child))
        return list(dict.fromkeys(messages))
    if isinstance(error, ValidationError):
        return ["입력/응답 스키마 불일치. 본문은 로그에 출력하지 않습니다."]
    if isinstance(error, KNOWN_ERRORS) or type(error) is ValueError or isinstance(error, OSError):
        message = SECRET.sub("[마스킹]", str(error))
        return [f"{type(error).__name__}: {message}"]
    return [f"{type(error).__name__}: 처리되지 않은 오류입니다. 본문은 출력하지 않습니다."]
