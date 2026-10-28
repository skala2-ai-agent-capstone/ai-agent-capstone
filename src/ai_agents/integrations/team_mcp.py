"""팀 전용 도구 두 개만 허용한다. 개인 대화/평가 조회·기준 생성 도구는 허용하지 않는다."""

from ..observability.tracing import trace_span
from .mcp_client import MCPError, MCPGateway, UnwrapTaskGroupErrors

TEAM_TOOLS = frozenset({"get_team_evaluation_snapshot", "save_team_evaluation_report"})


class TeamMCPGateway(UnwrapTaskGroupErrors, MCPGateway):
    def __init__(self, token, business_url):
        super().__init__(token, business_url=business_url, rag_url="")
        self._urls = {"business": business_url}

    async def call(self, server, tool, arguments):
        if server != "business" or tool not in TEAM_TOOLS:
            raise MCPError("TOOL_NOT_ALLOWED", tool)
        with trace_span(f"team.mcp.{tool}", inputs=arguments, run_type="tool") as span:
            result = await self._call(server, tool, arguments)
            span.result(result)
            return result
