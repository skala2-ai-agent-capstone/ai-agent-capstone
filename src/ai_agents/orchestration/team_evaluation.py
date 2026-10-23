"""팀 집계 조회 → 코칭 생성 → 검증된 결과 저장. MCP 도구 계약은 서버 구현 전 제안이다."""

from hashlib import sha256
from uuid import UUID

from ..evaluation_agent.team.report import build_report
from ..observability import trace_step
from ..shared.team_contracts import (
    TeamDecision,
    TeamImprovement,
    TeamReport,
    TeamSaveReceipt,
    TeamSaveRequest,
    TeamSnapshot,
)
from ..shared.workflow_models import content_hash


def build_save_request(snapshot: TeamSnapshot, report: TeamReport):
    # 체크포인트를 수정했거나 잘못된 수치/근거가 있으면 서버 전송 전에 거부한다.
    report = TeamReport.model_validate(report.model_dump(mode="json"))
    decision = TeamDecision(
        strengths=[row.metric_id for row in report.strengths],
        improvements=[TeamImprovement(metric_id=row.metric_id, action_id=row.action_id)
                      for row in report.improvements],
    )
    if build_report(snapshot, decision) != report:
        raise ValueError("team report does not match snapshot")
    key = sha256(f"team:{snapshot.request_id}:{snapshot.snapshot_id}".encode()).hexdigest()
    return TeamSaveRequest(
        request_id=snapshot.request_id, snapshot_id=snapshot.snapshot_id,
        idempotency_key=key, report_sha256=content_hash(report), report=report,
    )


class TeamEvaluationWorkflow:
    def __init__(self, gateway, agent):
        self.gateway, self.agent = gateway, agent

    @trace_step("team.prepare")
    async def prepare(self, request_id: UUID):
        request_id = UUID(str(request_id))
        envelope = await self.gateway.call("business", "get_team_evaluation_snapshot", {
            "request_id": str(request_id),
        })
        if envelope.get("ok") is not True:
            raise ValueError("team snapshot query failed")
        snapshot = TeamSnapshot.model_validate(envelope["data"])
        if snapshot.request_id != request_id:
            raise ValueError("team request mismatch")
        report = await self.agent.generate(snapshot)
        build_save_request(snapshot, report)
        # 호출자는 저장 전에 이 쌍을 영속화하고 저장 재시도에 재사용한다.
        return snapshot, report

    @trace_step("team.save")
    async def save(self, snapshot, report):
        request = build_save_request(snapshot, report)
        envelope = await self.gateway.call("business", "save_team_evaluation_report",
                                           request.model_dump(mode="json"))
        if envelope.get("ok") is not True:
            raise ValueError("team report save failed")
        receipt = TeamSaveReceipt.model_validate(envelope["data"])
        if (receipt.request_id != request.request_id
                or receipt.snapshot_id != request.snapshot_id
                or receipt.report_sha256 != request.report_sha256):
            raise ValueError("team save receipt mismatch")
        return receipt
