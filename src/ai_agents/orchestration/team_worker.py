"""팀 평가 Worker.

RabbitMQ와 독립적으로 검증할 수 있는 수신·생성·저장·재시도·DLQ 처리다.
요청마다 백엔드가 공급한 team 토큰(토큰 파일, request_id별)으로 business-mcp에서 고정 스냅샷을 읽고,
TeamAgent로 리포트를 만든 뒤 save_team_evaluation_report로 저장한다.
스냅샷·리포트는 저장 전에 체크포인트로 남기고, 재시도 때는 LLM을 다시 부르지 않고
같은 리포트로 저장만 다시 한다
(docs/team_evaluation.md 「Agent 연동 API」). 완료는 MCP 저장으로 끝나며 결과 큐는 없다.
재시도 한도를 넘기거나 처리할 수 없는 요청은 team_evaluation.dlq로 보낸다(백엔드가 실패로 기록).
"""

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import SecretStr, ValidationError

from ..evaluation_agent.team.agent import TeamEvaluationError
from ..integrations.final_tokens import FinalTokenError
from ..integrations.mcp_client import MCPError
from ..integrations.team_mcp import TeamMCPGateway
from ..observability import TraceSession
from ..shared.team_contracts import TeamReport, TeamSnapshot
from ..shared.team_job_contracts import DLQ, RETRY_QUEUE, TeamEvaluateEvent
from .team_evaluation import TeamEvaluationWorkflow

MAX_EVENT_BYTES = 16384
# MCP 오류 코드 → (Worker 오류 코드, 재시도 여부). 없는 코드는 MCP_REJECTED로 멈춘다.
MCP_ERRORS = {
    "TEMPORARY_FAILURE": ("MCP_UNAVAILABLE", True),
    "TRANSPORT_ERROR": ("MCP_UNAVAILABLE", True),
    "CONNECTION_FAILED": ("MCP_UNAVAILABLE", True),
    "NOT_FOUND": ("REQUEST_NOT_FOUND", False),
    # 토큰 서명·만료·종류·요청 범위가 맞지 않음. 백엔드가 새 토큰을 공급할 때까지 기다리지 않는다
    "FORBIDDEN": ("TOKEN_REJECTED", False),
    # 이 요청에 이미 다른 리포트가 저장됨. 다시 만들려면 새 요청이 필요하다
    "VERSION_CONFLICT": ("REPORT_CONFLICT", False),
    "VALIDATION_ERROR": ("REPORT_REJECTED", False),
    "SNAPSHOT_CHANGED": ("SNAPSHOT_CHANGED", False),
}


class TeamWorkerError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def classify(exc: Exception) -> TeamWorkerError:
    """실행 중 예외를 Worker 오류 코드로 바꾼다. 원문 메시지는 내보내지 않는다."""
    if isinstance(exc, TeamWorkerError):
        return exc
    if isinstance(exc, (FinalTokenError, TeamEvaluationError)):
        return TeamWorkerError(exc.code, retryable=exc.retryable)
    if isinstance(exc, MCPError):
        code, retryable = MCP_ERRORS.get(exc.code, ("MCP_REJECTED", False))
        return TeamWorkerError(code, retryable=retryable)
    if isinstance(exc, (ValidationError, ValueError)):
        return TeamWorkerError("INVALID_RESULT")
    return TeamWorkerError("UNEXPECTED_ERROR")


class TeamCheckpoints:
    """요청별 스냅샷·리포트.

    저장 응답이 유실돼도 같은 리포트로 다시 저장한다(같은 키·같은 내용이면 MCP가 기존 영수증).
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, request_id: UUID) -> Path:
        return self.directory / f"{UUID(str(request_id))}.json"

    def load(self, request_id: UUID):
        path = self.path(request_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            snapshot = TeamSnapshot.model_validate(raw["snapshot"])
            report = TeamReport.model_validate(raw["report"])
        except (OSError, ValueError, KeyError, TypeError):
            raise TeamWorkerError("CHECKPOINT_INVALID") from None
        if snapshot.request_id != UUID(str(request_id)) or report.request_id != snapshot.request_id:
            raise TeamWorkerError("CHECKPOINT_INVALID")
        return snapshot, report

    def save(self, snapshot: TeamSnapshot, report: TeamReport) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".team-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path(snapshot.request_id))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class TeamJobExecutor:
    """요청 하나: 토큰 → (체크포인트 또는 스냅샷 조회·리포트 생성·체크포인트) → 저장.

    직접 DB/내부 API 호출 없음.
    """

    def __init__(
        self, token_provider, agent, *, business_url, checkpoint_dir, gateway_factory=TeamMCPGateway
    ):
        self.token_provider, self.agent, self.business_url = token_provider, agent, business_url
        self.checkpoints = TeamCheckpoints(checkpoint_dir)
        self.gateway_factory = gateway_factory

    async def __call__(self, event: TeamEvaluateEvent):
        token = await self.token_provider(event)
        if not isinstance(token, SecretStr) or not token.get_secret_value().strip():
            raise ValueError("token provider must return a nonempty SecretStr")
        async with self.gateway_factory(token.get_secret_value(), self.business_url) as gateway:
            workflow = TeamEvaluationWorkflow(gateway, self.agent)
            saved = self.checkpoints.load(event.request_id)
            if saved is None:
                snapshot, report = await workflow.prepare(event.request_id)
                self.checkpoints.save(snapshot, report)
            else:
                snapshot, report = saved
            receipt = await workflow.save(snapshot, report)
        return {
            "report_status": report.status,
            "revision": receipt.revision,
            "resumed": saved is not None,
        }


class TeamWorker:
    def __init__(
        self,
        execute,
        *,
        trace_dir,
        model: str,
        code_provenance: dict,
        langsmith=False,
        max_retries=3,
    ):
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max retries must be 0..10")
        self.execute, self.trace_dir, self.model = execute, trace_dir, model
        self.code_provenance, self.langsmith, self.max_retries = (
            code_provenance,
            langsmith,
            max_retries,
        )

    async def handle(self, body: bytes, *, publish, ack):
        try:
            if len(body) > MAX_EVENT_BYTES:
                raise ValueError("oversized event")
            event = TeamEvaluateEvent.model_validate_json(body)
        except (ValidationError, ValueError):
            # 원본은 DLQ에 복사하지 않는다. 요청 ID만 읽히면 백엔드가 실패로 기록할 수 있게 남긴다.
            diagnostic = {"message_id": str(uuid4()), "type": "INVALID_TEAM_EVALUATE"}
            if request_id := _request_id(body):
                diagnostic["request_id"] = request_id
            await publish(DLQ, diagnostic, "INVALID_EVENT")
            await ack()
            return {"status": "dlq", "code": "INVALID_EVENT"}

        with TraceSession(
            self.trace_dir,
            langsmith=self.langsmith,
            metadata={
                "request_id": str(event.request_id),
                "purpose": "team",
                "model": self.model,
                "git_commit": self.code_provenance.get("git_commit", "unknown"),
            },
        ) as trace:
            with trace.span("team.worker", inputs=event) as span:
                failure, done = None, None
                try:
                    if event.attempt > self.max_retries:
                        raise TeamWorkerError("RETRY_EXHAUSTED")
                    done = await self.execute(event)
                except Exception as exc:
                    failure = classify(exc)
                if failure:
                    retry = failure.retryable and event.attempt < self.max_retries
                    payload = event.model_dump(mode="json")
                    if retry:
                        payload["attempt"] += 1
                    # 브로커의 발행 확인까지 기다린다. 실패 시 원본 메시지를 ack하지 않는다.
                    await publish(RETRY_QUEUE if retry else DLQ, payload, failure.code)
                    result = {"status": "retry" if retry else "dlq", "code": failure.code}
                else:
                    result = {"status": "saved", **done}
                result.update(request_id=str(event.request_id), trace_id=trace.trace_id)
                span.result(result)
                span.outputs["status"] = result["status"]
                if failure:
                    span.outputs["error_code"] = failure.code
            # MCP 저장, 또는 재시도/DLQ 발행 확인 뒤에만 ack한다.
            await ack()
            return result


def _request_id(body: bytes) -> str | None:
    # 크기 초과·깊은 중첩 본문을 다시 파싱하다 예외가 밖으로 나가면 nack·재전달이 끝없이 반복된다
    if len(body) > MAX_EVENT_BYTES:
        return None
    try:
        value = json.loads(body).get("request_id")
        return str(UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError, TypeError, RecursionError):
        return None
