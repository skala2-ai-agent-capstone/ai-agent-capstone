"""백엔드가 발행하는 최종 평가 작업과 저장 완료 알림. 토큰·원문은 큐에 싣지 않는다."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .live_feedback_contracts import ID
from .models import StrictModel


class FinalEvaluationEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    type: Literal["FINAL_EVALUATE"] = "FINAL_EVALUATE"
    message_id: ID
    evaluation_id: ID
    operation_id: ID
    attempt: int = Field(default=0, ge=0, le=100, strict=True)


class BackendEvaluationScope(StrictModel):
    company_id: ID
    task_id: ID
    deliverable_version: ID
    rubric_version: ID


class BackendEvaluationEvent(BackendEvaluationScope):
    type: Literal["EVALUATE"]
    message_id: ID
    evaluation_id: ID
    attempt: int = Field(default=0, ge=0, le=100, strict=True)

    def normalized(self):
        scope = BackendEvaluationScope.model_validate(self.model_dump(include={
            "company_id", "task_id", "deliverable_version", "rubric_version",
        }))
        # 재발행 message_id나 재시도 횟수가 바뀌어도 같은 업무/평가/기준은 같은 실행이다.
        identity = {"evaluation_id": self.evaluation_id, **scope.model_dump()}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return BackendFinalEvaluationEvent(
            message_id=self.message_id, evaluation_id=self.evaluation_id,
            operation_id=f"backend-v1:{key}", attempt=self.attempt, backend_scope=scope,
        )


class BackendFinalEvaluationEvent(FinalEvaluationEvent):
    backend_scope: BackendEvaluationScope


@dataclass(frozen=True)
class FinalQueueContract:
    name: str
    requests: str
    retry: str
    dlq: str
    results: str


FINAL_QUEUE_CONTRACTS = {
    "agent-v1": FinalQueueContract(
        "agent-v1", "final_evaluation.requests", "final_evaluation.retry",
        "final_evaluation.dlq", "final_evaluation.results",
    ),
    "backend-v1": FinalQueueContract(
        "backend-v1", "evaluation.requests", "evaluation.retry", "evaluation.dlq",
        "evaluation.results",
    ),
}


class FinalEvaluationSaved(StrictModel):
    schema_version: Literal["1"] = "1"
    type: Literal["FINAL_EVALUATION_SAVED"] = "FINAL_EVALUATION_SAVED"
    message_id: ID
    evaluation_id: ID
    operation_id: ID
    status: Literal["draft_and_coaching_saved"] = "draft_and_coaching_saved"
    review_required: bool = Field(strict=True)
    draft_revision: int = Field(ge=1, strict=True)
    coaching_revision: int = Field(ge=1, strict=True)
