"""평가 입력 준비. GPT 평가·리포트는 후속 단위에서 연결하며 DB에는 저장하지 않는다."""

from ...integrations.mcp_adapter import BusinessToolCaller, collect_evaluation_evidence
from ...shared.evidence_models import EvaluationEvidence, EvaluationRun
from ...shared.workflow_models import EvaluationInput, content_hash
from .stored_rubric import StoredEvaluationInput, checked_input


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
    if isinstance(checked, StoredEvaluationInput):
        # 저장 기준 경로에서는 WorkContext가 메타데이터일 뿐이다. 빈 기본 목록을
        # 실제 대화·산출물 미수집으로 오인하지 않도록 MCP 근거와 구분한다.
        payload["work_record"] = checked.work_record.model_dump(
            mode="json", include={"work_id", "title"},
        )
        definitions = {row.item_id: row.criterion for row in checked.stored_rubric.items}
        payload["criterion_definitions"] = {
            key: definitions[item_id] for key, item_id in checked.stored_rubric.item_ids().items()
        }
    if evidence is not None:
        evidence = EvaluationEvidence.model_validate(evidence.model_dump(mode="json"))
        _check_run(checked, evidence.context)
        target = next(a for a in evidence.artifacts if a.is_evaluation_target)
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
                "collected_evidence_facts": {
                    "target_artifact": {
                        "artifact_id": target.artifact_id,
                        "version": target.version,
                        "saved_at": target.saved_at.isoformat(),
                        "author": target.author,
                        "content_present": bool(target.content.strip()),
                        "span_ids": [span.span_id for span in target.spans],
                    },
                    "registration_available": evidence.work_case.registration is not None,
                    "policy_check_available": evidence.policy_check is not None,
                },
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


def compact_evaluation_payload(payload: dict) -> dict:
    """모델에는 검증된 선택 목록을 한 번만 전달하고 실제 업무 근거를 먼저 배치한다."""
    return {
        "work_evidence_catalog": payload["work_evidence_catalog"],
        "collected_evidence_facts": payload["collected_evidence_facts"],
        "security_evidence": payload["security_evidence"],
        "missing_sources": payload["missing_sources"],
        "criteria": [
            {
                **{key: value for key, value in item.items()
                   if key not in {"rule_evidence", "evidence_sources"}},
                **({"criterion": payload["criterion_definitions"][item["id"]]}
                   if item["id"] in payload.get("criterion_definitions", {}) else {}),
            }
            for item in payload["criteria"]["items"]
        ],
        "criterion_requirements": payload["criterion_requirements"],
        "reference_catalog": payload["reference_catalog"],
        # 비교 결과와 문서 상태는 선택 목록에 없는 정보이므로 유지한다.
        "additional_details": {
            key: value for key, value in payload["additional_details"].items()
            if key in {"artifact_diffs", "reference_status"}
        },
        "run_id": payload["run_id"],
        "criteria_sha256": payload["criteria_sha256"],
        "work_type": payload["work_type"],
        "work_record": {key: value for key, value in payload["work_record"].items()
                        if key in {"work_id", "title"}},
        "snapshot": payload["snapshot"],
        "evidence_sha256": payload["evidence_sha256"],
        "evidence_selection_version": payload["evidence_selection_version"],
    }


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
