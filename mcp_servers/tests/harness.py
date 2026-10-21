"""테스트 하네스: 실행 토큰 발급(내부 API)과 MCP 도구 호출 (공식 MCP Python SDK 클라이언트 사용)."""
from __future__ import annotations

import json
import os
import urllib.request
from contextlib import AsyncExitStack

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

BUSINESS = os.environ.get("BUSINESS_MCP_URL", "http://localhost:8001")
RAG = os.environ.get("RAG_MCP_URL", "http://localhost:8002")
INTERNAL_KEY = os.environ.get("INTERNAL_API_KEY", "")


def internal(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BUSINESS + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Internal-Key": INTERNAL_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def issue_run(evaluation_id: str, **kw) -> dict:
    res = internal("POST", "/internal/runs", {"evaluation_id": evaluation_id, **kw})
    assert res["ok"], res
    return res["data"]


class Session:
    """한 실행 토큰으로 두 MCP 서버에 붙는 클라이언트."""

    def __init__(self, token: str):
        self.token = token
        self.clients: dict[str, Client] = {}
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        await self.stack.__aenter__()
        for name, base in (("business", BUSINESS), ("rag", RAG)):
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {self.token}"})
            self.clients[name] = await self.stack.enter_async_context(
                Client(streamable_http_client(base + "/mcp", http_client=http))
            )
        return self

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)

    async def call(self, server: str, tool: str, args: dict | None = None) -> dict:
        res = await self.clients[server].call_tool(tool, args or {})
        if res.structured_content is not None:
            sc = res.structured_content
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        return json.loads(res.content[0].text)

    async def list_tools(self, server: str) -> list[str]:
        r = await self.clients[server].list_tools()
        return [t.name for t in r.tools]
