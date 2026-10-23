"""직원 업무 평가와 분리된 내규 조회·평가기준 생성 진입점."""

from ..integrations.mcp_client import ToolGateway
from ..integrations.mcp_data import fetch_policy
from ..shared.execution_contracts import DocumentRef
from ..shared.workflow_models import CriteriaDraft
from .builder import OpenAIRubricBuilder, canonical_work_type


async def generate_criteria(
    gateway: ToolGateway,
    builder: OpenAIRubricBuilder,
    work_type: str,
    *,
    expected_policy: DocumentRef | None = None,
) -> CriteriaDraft:
    """한 업무 유형의 기준을 생성·검증해 반환한다. 승인·저장은 호출부에서 처리한다."""
    work_type = canonical_work_type(work_type)
    policy = await fetch_policy(gateway, work_type, expected_policy=expected_policy)
    return await builder.generate(work_type, policy)
