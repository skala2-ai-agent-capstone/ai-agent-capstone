"""평가 기준 생성 전용 MCP 클라이언트.

내규 조회(search_policy)와 초안 저장(save_rubric)만 허용한다.
"""

import base64
import json

from ..observability.tracing import trace_span
from .mcp_client import MCPError, MCPGateway, UnwrapTaskGroupErrors

RUBRIC_TOOLS = {"rag": frozenset({"search_policy"}), "business": frozenset({"save_rubric"})}


def token_claims(token: str) -> dict:
    """실행 토큰의 claims를 서명 확인 없이 읽는다. 회사·종류·만료를 요청과 대조하는 사전 검사용이며
    권한 확인은 MCP가 서명으로 한다. 형식이 틀리면 빈 dict."""
    try:
        payload = token.strip().split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError):
        return {}
    return claims if isinstance(claims, dict) else {}


class RubricMCPGateway(UnwrapTaskGroupErrors, MCPGateway):
    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        if tool not in RUBRIC_TOOLS.get(server, ()):
            raise MCPError("TOOL_NOT_ALLOWED", tool)
        # 저장 인자는 기준표 전체라 추적에는 유형·키만 남긴다.
        inputs = arguments if tool == "search_policy" else {
            k: arguments.get(k) for k in ("work_type", "idempotency_key")
        }
        with trace_span(f"rubric.mcp.{tool}", inputs=inputs, run_type="tool") as span:
            result = await self._call(server, tool, arguments)
            span.result(result.get("data"))
            return result
