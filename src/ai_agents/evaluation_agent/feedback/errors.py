"""로그/DLQ에 원문 예외 메시지 대신 사용하는 고정 오류 코드."""

from typing import Literal

ErrorCode = Literal[
    "INVALID_EVENT",
    "CONTRACT_MISMATCH",
    "SCOPE_MISMATCH",
    "EMPTY_CONVERSATION",
    "INCOMPLETE_EXCHANGE",
    "INVALID_SEQUENCE",
    "INPUT_TOO_LARGE",
    "UNMASKED_INPUT",
    "INVALID_FEEDBACK",
    "SENSITIVE_OUTPUT",
    "MCP_UNAVAILABLE",
    "MCP_REJECTED",
    "LLM_UNAVAILABLE",
    "LLM_REJECTED",
    "UNEXPECTED_ERROR",
]


class FeedbackError(Exception):
    def __init__(self, code: ErrorCode, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
