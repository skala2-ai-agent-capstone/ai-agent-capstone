"""직원 업무 평가와 분리된 내규 조회·평가기준 생성 진입점."""

from ..integrations.mcp_client import ToolGateway
from ..integrations.mcp_data import fetch_policy
from ..shared.execution_contracts import DocumentRef
from ..shared.workflow_models import CriteriaDraft
from .builder import OpenAIRubricBuilder, RubricError, canonical_work_type
from .mcp_output import to_mcp_rubric
from .policy import PolicyBundle


async def generate_criteria(
    gateway: ToolGateway,
    builder: OpenAIRubricBuilder,
    work_type: str,
    *,
    expected_policy: DocumentRef | None = None,
) -> CriteriaDraft:
    """한 업무 유형의 기준을 생성·검증해 반환한다. 승인·저장은 호출부에서 처리한다."""
    draft, _ = await _generate_with_policy(gateway, builder, work_type, expected_policy)
    return draft


async def generate_mcp_criteria(
    gateway: ToolGateway,
    builder: OpenAIRubricBuilder,
    work_type: str,
    *,
    expected_policy: DocumentRef | None = None,
) -> dict[str, object]:
    """내규 조회·생성·검증 후 MCP 형식의 work_type·items를 반환한다.

    idempotency_key·item_id를 생성하거나 save_rubric을 호출하지 않는다.
    내부 근거·경고는 generate_criteria가 반환하는 CriteriaDraft에서 확인한다.
    """
    draft, policy = await _generate_with_policy(gateway, builder, work_type, expected_policy)
    return to_mcp_rubric(draft, policy)


async def _generate_with_policy(
    gateway: ToolGateway,
    builder: OpenAIRubricBuilder,
    work_type: str,
    expected_policy: DocumentRef | None,
) -> tuple[CriteriaDraft, PolicyBundle]:
    work_type = canonical_work_type(work_type)
    policy = await fetch_policy(gateway, work_type, expected_policy=expected_policy)
    draft = await builder.generate(work_type, policy)
    if draft.work_type != work_type:
        raise RubricError("요청한 work_type과 생성 결과의 업무 유형이 다릅니다.")
    return draft, policy
