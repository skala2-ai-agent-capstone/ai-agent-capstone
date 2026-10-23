"""팀 평가 큐 계약. 백엔드 EvaluationMessages.TeamEvaluate·team_evaluation.* 큐와 같은 칸이다.

요청(TEAM_EVALUATE)은 백엔드가 team_evaluation.requests로 보낸다. 팀·기간·공개 승인과 집계 스냅샷은
business-mcp가 요청을 만들 때 request_id에 고정했으므로 메시지에는 식별자만 싣는다.
완료는 Worker가 save_team_evaluation_report로 저장하는 것으로 끝난다
(MCP가 team_evaluations를 완료로 바꿈).
결과 큐는 없고, 실패만 team_evaluation.dlq로 보낸다(error_code 헤더).
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from .live_feedback_contracts import ID
from .models import StrictModel

REQUEST_QUEUE = "team_evaluation.requests"
RETRY_QUEUE = "team_evaluation.retry"
DLQ = "team_evaluation.dlq"


class TeamEvaluateEvent(StrictModel):
    message_id: ID
    type: Literal["TEAM_EVALUATE"]
    request_id: UUID
    company_id: ID
    attempt: int = Field(default=0, ge=0, le=100, strict=True)
