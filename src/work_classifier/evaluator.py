from .workflow_models import EvaluationInput


def build_evaluation_payload(
    request: EvaluationInput,
) -> dict[str, object]:
    """승인 정보를 확인하고 AI 평가에 전달할 자료를 정리한다."""

    checked = EvaluationInput.model_validate(
        request.model_dump(mode="json")
    )

    return {
        "run_id": checked.run_id,
        "criteria_sha256": checked.approval.criteria_sha256,
        "work_type": checked.classification.work_type,
        "work_record": checked.work_record.model_dump(mode="json"),
        "criteria": checked.approved_criteria.model_dump(mode="json"),
    }
