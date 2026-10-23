"""최종 평가 전용 허용 목록. 기준 생성·확정·팀 집계 권한은 추가하지 않는다."""

from ..observability.tracing import trace_span
from .mcp_client import READ_TOOLS, MCPError, MCPGateway

FINAL_TOOLS = {
    **READ_TOOLS,
    "business": READ_TOOLS["business"]
    | {
        "get_evaluation_context",
        "save_evaluation_draft",
        "save_coaching",
    },
}


class FinalMCPGateway(MCPGateway):
    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        if tool not in FINAL_TOOLS.get(server, ()):
            raise MCPError("TOOL_NOT_ALLOWED", tool)
        with trace_span(f"final.mcp.{tool}", inputs=arguments, run_type="tool") as span:
            result = await self._call(server, tool, arguments)
            span.result(result)
            return result
