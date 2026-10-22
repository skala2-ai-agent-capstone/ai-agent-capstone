"""MCP SDK 2.x 연결. 실행 토큰은 클라이언트에만 보관하고 그래프에 넣지 않는다."""

import asyncio
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from typing import Protocol

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from ..observability.tracing import trace_span

READ_TOOLS = {
    "business": frozenset({
        "get_work_case", "get_conversation", "get_source_document", "get_artifact_version",
        "get_artifact_diff", "get_linked_works", "get_reference_access_log",
        "get_evidence_links", "get_approval_requests", "get_policy_check_result",
        "compute_reference_quote",
    }),
    "rag": frozenset({"search_policy", "search_reference", "get_reference",
                      "search_rubric", "search_examples"}),
}


class MCPError(ValueError):
    def __init__(self, code: str, tool: str):
        self.code, self.tool = code, tool
        # 서버 오류 본문에는 고객 데이터가 들어갈 수 있으므로 그대로 출력하지 않는다.
        super().__init__(f"MCP {tool}: {code}")


class ToolGateway(Protocol):
    async def call(self, server: str, tool: str, arguments: dict) -> dict: ...


def unpack_result(result) -> dict:
    if result.is_error:
        raise MCPError("PROTOCOL_ERROR", "call_tool")
    value = result.structured_content
    if value is None:
        texts = [c.text for c in result.content if c.type == "text"]
        if len(texts) != 1:
            raise MCPError("INVALID_ENVELOPE", "call_tool")
        try:
            value = json.loads(texts[0])
        except (TypeError, ValueError):
            raise MCPError("INVALID_ENVELOPE", "call_tool") from None
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise MCPError("INVALID_ENVELOPE", "call_tool")
    return value


class MCPGateway:
    def __init__(self, token: str, *, business_url: str, rag_url: str, sleep=asyncio.sleep):
        if not token.strip():
            raise MCPError("TOKEN_REQUIRED", "connect")
        self._token = token
        self._urls = {"business": business_url, "rag": rag_url}
        self._clients = {}
        self._stack = AsyncExitStack()
        self._sleep = sleep
        self._scope = None

    async def __aenter__(self):
        try:
            for name, url in self._urls.items():
                http = await self._stack.enter_async_context(create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self._token}"},
                ))
                self._clients[name] = await self._stack.enter_async_context(Client(
                    streamable_http_client(url, http_client=http), read_timeout_seconds=60,
                ))
        except Exception:
            await self._stack.aclose()
            raise MCPError("CONNECTION_FAILED", "connect") from None
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        if tool not in READ_TOOLS.get(server, ()):
            raise MCPError("TOOL_NOT_ALLOWED", tool)
        with trace_span(f"mcp.{server}.{tool}", inputs=arguments, run_type="tool") as span:
            result = await self._call(server, tool, arguments)
            span.result(result)
            return result

    async def _call(self, server: str, tool: str, arguments: dict) -> dict:
        if server not in self._clients:
            raise MCPError("NOT_CONNECTED", tool)
        for attempt in range(4):  # 최초 1회 + TEMPORARY_FAILURE 재시도 최대 3회
            try:
                with trace_span("mcp.attempt", inputs={"attempt": attempt},
                                run_type="tool") as span:
                    result = await self._clients[server].call_tool(tool, arguments)
                    span.result(result)
            except Exception:
                raise MCPError("TRANSPORT_ERROR", tool) from None
            envelope = unpack_result(result)
            if envelope["ok"]:
                meta = envelope.get("meta", {})
                scope = {"run_id": meta.get("run_id"), "snapshot": meta.get("snapshot")}
                if (not isinstance(envelope.get("data"), dict)
                        or not isinstance(scope["run_id"], str) or not scope["run_id"]
                        or not isinstance(scope["snapshot"], dict)):
                    raise MCPError("INVALID_ENVELOPE", tool)
                if self._scope is not None and scope != self._scope:
                    raise MCPError("SNAPSHOT_CHANGED", tool)
                self._scope = deepcopy(scope)
                return envelope
            error = envelope.get("error", {})
            code = error.get("code", "INVALID_ENVELOPE")
            if code == "TEMPORARY_FAILURE" and error.get("retryable") is True and attempt < 3:
                await self._sleep(0.5 * 2 ** attempt)
                continue
            # 고정 스냅샷으로 호출하므로 VERSION_CONFLICT는 임의 최신 버전으로 대체하지 않는다.
            raise MCPError(code, tool)
        raise AssertionError("unreachable")
