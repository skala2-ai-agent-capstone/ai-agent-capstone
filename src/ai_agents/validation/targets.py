"""네트워크 없이 실행하는 계약 검증 예제. AI 품질 측정 결과가 아니다."""

from ..shared.execution_contracts import EvaluationRequest


def validate_request(inputs: dict) -> dict:
    request = EvaluationRequest.model_validate(inputs)
    return {
        "purpose": request.purpose,
        "work_type": request.work_type,
        "snapshot_id": request.snapshot.snapshot_id,
        "has_artifact": request.snapshot.artifact_ref is not None,
    }
