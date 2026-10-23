"""평가 입력 준비. GPT 평가·리포트는 후속 단위에서 연결하며 DB에는 저장하지 않는다."""

from ...integrations.mcp_adapter import BusinessToolCaller, collect_evaluation_evidence
from ...shared.evidence_models import EvaluationEvidence, EvaluationRun
from ...shared.workflow_models import EvaluationInput, content_hash
from .stored_rubric import checked_input


def build_evaluation_payload(
    request: EvaluationInput,
    *,
    evidence: EvaluationEvidence | None = None,
) -> dict[str, object]:
    """승인 정보를 확인하고 AI 평가에 전달할 자료를 정리한다."""

    checked = checked_input(request)

    payload = {
        "run_id": checked.run_id,
        "criteria_sha256": content_hash(checked.approved_criteria),
        "work_type": checked.classification.work_type,
        "work_record": checked.work_record.model_dump(mode="json"),
        "criteria": checked.approved_criteria.model_dump(mode="json"),
    }
    if evidence is not None:
        evidence = EvaluationEvidence.model_validate(evidence.model_dump(mode="json"))
        _check_run(checked, evidence.context)
        sources = checked.work_record.evidence_sources()
        for source_id, text in evidence.evidence_sources().items():
            if source_id in sources and sources[source_id] != text:
                raise ValueError("같은 근거 ID에 서로 다른 원문이 연결되었습니다.")
            sources[source_id] = text
        payload.update(
            {
                "snapshot": evidence.context.snapshot,
                "evidence": evidence.model_dump(mode="json"),
                "evidence_sha256": content_hash(evidence),
                "work_evidence_sources": sources,
                "missing_sources": sorted(
                    set(checked.work_record.missing_sources + evidence.missing_sources)
                ),
            }
        )
    return payload


def _check_run(request: EvaluationInput, context: EvaluationRun) -> None:
    if request.run_id != context.run_id or request.work_record.work_id != context.work_id:
        raise ValueError("승인된 입력과 MCP 근거의 실행 또는 업무가 다릅니다.")


async def prepare_mcp_evaluation_payload(
    request: EvaluationInput,
    *,
    context: EvaluationRun,
    call_tool: BusinessToolCaller,
) -> dict[str, object]:
    """승인 확인 → MCP 읽기 도구 → 근거 변환 → 평가 입력 연결."""
    request = EvaluationInput.model_validate(request.model_dump(mode="json"))
    context = EvaluationRun.model_validate(context.model_dump(mode="json"))
    _check_run(request, context)
    evidence = await collect_evaluation_evidence(context=context, call_tool=call_tool)
    return build_evaluation_payload(request, evidence=evidence)
