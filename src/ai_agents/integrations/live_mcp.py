"""중간 피드백 전용 MCP 클라이언트. DB와 Spring을 직접 호출하지 않는다."""

from contextlib import AsyncExitStack

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from ..evaluation_agent.feedback.errors import FeedbackError
from ..observability.tracing import trace_span
from .mcp_client import MCPError, unpack_result

LIVE_TOOLS = frozenset({"start_live_evaluation", "get_messages", "save_live_evaluation"})


class LiveMCPGateway:
    def __init__(self, token: str, url: str):
        if not token.strip() or not url.startswith(("https://", "http://")):
            raise ValueError("feedback MCP configuration is required")
        self._token, self._url = token, url
        self._stack = AsyncExitStack()
        self._client = None

    async def __aenter__(self):
        try:
            http = await self._stack.enter_async_context(
                create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            )
            self._client = await self._stack.enter_async_context(
                Client(
                    streamable_http_client(self._url, http_client=http),
                    read_timeout_seconds=60,
                )
            )
        except Exception:
            await self._stack.aclose()
            raise FeedbackError("MCP_UNAVAILABLE", retryable=True) from None
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._stack.__aexit__(*exc)
        except BaseExceptionGroup as group:
            # MCP 클라이언트의 task group이 블록 안 예외를 ExceptionGroup으로 감싼다.
            # 그대로 내보내면 Worker가 오류 코드·재시도 여부를 잃고 UNEXPECTED_ERROR로
            # DLQ에 보내므로 안쪽 FeedbackError를 꺼내 올린다.
            inner = group.subgroup(FeedbackError)
            if inner is None:
                raise
            while isinstance(inner, BaseExceptionGroup):
                inner = inner.exceptions[0]
            raise inner from None

    async def call(self, tool: str, arguments: dict) -> dict:
        if tool not in LIVE_TOOLS or self._client is None:
            raise FeedbackError("MCP_REJECTED")
        with trace_span(f"feedback.mcp.{tool}", inputs=arguments, run_type="tool") as span:
            try:
                reply = await self._client.call_tool(tool, arguments)
            except Exception:
                raise FeedbackError("MCP_UNAVAILABLE", retryable=True) from None
            try:
                envelope = unpack_result(reply)
            except MCPError:
                raise FeedbackError("CONTRACT_MISMATCH") from None
            if envelope["ok"]:
                data = envelope.get("data")
                if not isinstance(data, dict):
                    raise FeedbackError("CONTRACT_MISMATCH")
                span.result(data)
                return data
            error = envelope.get("error", {})
            transient = (
                isinstance(error, dict)
                and error.get("code") == "TEMPORARY_FAILURE"
                and error.get("retryable") is True
            )
            raise FeedbackError(
                "MCP_UNAVAILABLE" if transient else "MCP_REJECTED", retryable=transient
            )
