"""저장 기준 조회 → 평가·근거 검증 → 로컬 체크포인트 → MCP 초안·코칭 저장.

서버 확정, 본인 확인, 관리자 조회, 팀 집계는 수행하지 않는다.
"""

import asyncio
import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field

from ..evaluation_agent.final.agent import OpenAIEvaluator
from ..evaluation_agent.final.materials import EvaluationMaterials, collect_evaluation_materials
from ..evaluation_agent.final.models import AgentEvaluationOutput
from ..evaluation_agent.final.persistence import save_arguments
from ..evaluation_agent.final.stored_rubric import StoredEvaluationInput, StoredRubric
from ..evaluation_agent.final.validation import EvaluationError
from ..integrations.mcp_adapter import collect_evaluation_evidence, read_mcp_response
from ..integrations.mcp_client import MCPError
from ..observability import trace_step
from ..shared.evidence_models import EvaluationEvidence, EvaluationRun
from ..shared.live_feedback_contracts import ID
from ..shared.models import StrictModel, WorkContext
from ..shared.work_selection import selected_work_type
from ..shared.workflow_models import content_hash

WRITABLE = {"대기", "수집 중", "평가 중", "근거 확인 중", "코칭 생성 중"}


class FinalEvaluationJob(StrictModel):
    evaluation_id: ID
    operation_id: ID  # 동일 논리 실행의 재시도에서는 유지해야 한다.

    def key(self):
        return content_hash(self)


class PreparedFinalEvaluation(StrictModel):
    job: FinalEvaluationJob
    model: str
    request: StoredEvaluationInput
    evidence: EvaluationEvidence
    materials: EvaluationMaterials
    report: AgentEvaluationOutput
    code_provenance: dict = Field(default_factory=dict)
    receipts: dict[str, dict] = Field(default_factory=dict)


class FinalCheckpoint:
    def __init__(self, directory: Path, job: FinalEvaluationJob):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / f"{job.key()}.json"
        self.lock_path = (
            directory / f"{hashlib.sha256(job.evaluation_id.encode()).hexdigest()}.lock"
        )

    @contextmanager
    def locked(self):
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EvaluationError("같은 최종 평가 실행이 이미 진행 중입니다.") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return None
        return PreparedFinalEvaluation.model_validate_json(self.path.read_text())

    def write(self, prepared):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".final-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(prepared.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)  # 0600. 저장 성공 응답이 유실되어도 같은 payload로 재시도.
        finally:
            if os.path.exists(name):
                os.unlink(name)


class FinalEvaluationWorkflow:
    def __init__(
        self,
        gateway,
        client,
        *,
        model: str,
        checkpoint_dir: Path,
        code_provenance=None,
        sleep=asyncio.sleep,
        api_mode="chat",
    ):
        self.gateway, self.client, self.model = gateway, client, model
        self.directory, self.sleep, self.api_mode = checkpoint_dir, sleep, api_mode
        self.code_provenance = code_provenance or {}

    @trace_step("final.evaluate_and_save")
    async def run(self, job: FinalEvaluationJob):
        job = FinalEvaluationJob.model_validate(job.model_dump())
        checkpoint = FinalCheckpoint(self.directory, job)
        with checkpoint.locked():
            reply = await self.gateway.call(
                "business",
                "get_evaluation_context",
                {"evaluation_id": job.evaluation_id},
            )
            success = read_mcp_response(reply)
            target = success.data
            if target.get("evaluation_id") != job.evaluation_id:
                raise EvaluationError("다른 평가 건의 실행 문맥입니다.")
            for field in ("draft_revision", "coaching_revision"):
                if field not in target or (
                    target[field] is not None
                    and (type(target[field]) is not int or target[field] < 1)
                ):
                    raise EvaluationError("평가 문맥에 유효한 저장 리비전 정보가 없습니다.")
            context = EvaluationRun(
                run_id=success.meta.run_id,
                work_id=target["work_id"],
                snapshot=success.meta.snapshot,
            )
            if (target["artifact_id"], target["artifact_version"]) != (
                context.target.artifact_id,
                context.target.version,
            ):
                raise EvaluationError("평가 건과 실행 토큰의 산출물 버전이 다릅니다.")
            prepared = checkpoint.read()
            if prepared is None:
                if (
                    target.get("draft_revision") is not None
                    or target.get("coaching_revision") is not None
                ):
                    raise EvaluationError(
                        "이미 저장된 결과가 있습니다. 기존 체크포인트로 재개하세요."
                    )
                if target["status"] not in WRITABLE:
                    raise EvaluationError("현재 서버 상태에서는 초안을 생성할 수 없습니다.")
                prepared = await self.prepare(job, context, target)
                save_arguments(prepared)  # 검증이 끝나기 전에는 저장 호출을 하지 않는다.
                checkpoint.write(prepared)
            else:
                if (
                    prepared.job != job
                    or prepared.model != self.model
                    or prepared.evidence.context.work_id != context.work_id
                    or prepared.evidence.context.snapshot != context.snapshot
                    or prepared.request.stored_rubric.version != target["rubric_version"]
                ):
                    raise EvaluationError("체크포인트와 현재 업무·기준·산출물·모델이 다릅니다.")
                for tool, receipt in prepared.receipts.items():
                    revision = {
                        "save_evaluation_draft": "draft_revision",
                        "save_coaching": "coaching_revision",
                    }.get(tool)
                    if revision is None or receipt.get(revision) != target[revision]:
                        raise EvaluationError(
                            "서버의 저장 리비전이 체크포인트 이후 변경되었습니다."
                        )
            payloads = save_arguments(prepared)
            for tool, payload in payloads.items():
                if tool in prepared.receipts:
                    self.check_receipt(tool, prepared.receipts[tool], job)
                    continue
                receipt = await self.save(tool, payload, context)
                self.check_receipt(tool, receipt, job)
                prepared.receipts[tool] = receipt
                checkpoint.write(prepared)
            return {
                "status": "draft_and_coaching_saved",
                "evaluation_id": job.evaluation_id,
                "rubric_doc_id": prepared.request.stored_rubric.doc_id,
                "rubric_version": prepared.request.stored_rubric.version,
                "criteria_sha256": prepared.report.criteria_sha256,
                "review_required": any(
                    i.review_required
                    or i.observation
                    in {
                        "needs_review",
                        "insufficient_data",
                    }
                    for i in prepared.report.items
                ),
                "receipts": prepared.receipts,
                "checkpoint": str(checkpoint.path),
            }

    async def prepare(self, job, context, target):
        work_data = {}

        async def call(server, name, arguments):
            reply = await self.gateway.call(server, name, arguments)
            read_mcp_response(reply, context=context)
            return reply

        async def business(name, arguments):
            reply = await call("business", name, arguments)
            if name == "get_work_case":
                work_data.update(reply["data"])
            return reply

        evidence = await collect_evaluation_evidence(context=context, call_tool=business)
        selection = selected_work_type(work_data.get("work_type"))
        response = await call(
            "rag",
            "search_rubric",
            {
                "work_type": selection.registered_work_type,
                "rubric_version": target["rubric_version"],
            },
        )
        rubric = StoredRubric.model_validate(response["data"])
        if (rubric.work_type, rubric.version) != (
            selection.registered_work_type,
            target["rubric_version"],
        ):
            raise EvaluationError("조회된 기준의 업무 유형 또는 고정 버전이 다릅니다.")
        criteria = rubric.criteria()
        refs = {(r.document_id, r.version, r.section_id): r for r in criteria.references}

        async def rag(name, arguments):
            key = tuple(arguments.get(k) for k in ("doc_id", "version", "chunk_id"))
            if name == "get_reference" and key in refs:
                # get_reference는 루브릭 컬렉션을 지원하지 않는다. 이미 정확 조회한 원본을 사용한다.
                ref = refs[key]
                return {
                    "ok": True,
                    "data": {
                        **arguments,
                        "ref": f"{ref.document_id}@{ref.version}#{ref.section_id}",
                        "text": ref.content,
                    },
                    "meta": response["meta"],
                }
            return await call("rag", name, arguments)

        materials = await collect_evaluation_materials(
            evidence=evidence,
            criteria=criteria,
            business_call=business,
            rag_call=rag,
        )
        request = StoredEvaluationInput(
            run_id=context.run_id,
            work_record=WorkContext(
                work_id=context.work_id,
                title=work_data.get("title") or context.work_id,
                coverage="complete",
            ),
            classification=selection,
            stored_rubric=rubric,
            approved_criteria=criteria,
        )
        if len(context.work_id) > 128:
            raise EvaluationError("프록시 기록용 업무 ID 길이를 초과했습니다.")
        agent = OpenAIEvaluator(
            self.client,
            context=context,
            business_call=business,
            rag_call=rag,
            model=self.model,
            api_mode=self.api_mode,
            extra_headers={"X-Stage": "evaluate", "X-Work-Id": context.work_id},
        )
        report = await agent.evaluate_collected(request, evidence=evidence, materials=materials)
        return PreparedFinalEvaluation(
            job=job,
            model=self.model,
            request=request,
            evidence=evidence,
            materials=materials,
            report=report,
            code_provenance=self.code_provenance,
        )

    async def save(self, tool, arguments, context):
        for attempt in range(3):
            try:
                reply = await self.gateway.call("business", tool, arguments)
                return read_mcp_response(reply, context=context).data
            except MCPError as exc:
                if exc.code not in {"TRANSPORT_ERROR", "TEMPORARY_FAILURE"} or attempt == 2:
                    raise
                await self.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def check_receipt(tool, data, job):
        revision = "draft_revision" if tool == "save_evaluation_draft" else "coaching_revision"
        if (
            data.get("evaluation_id") != job.evaluation_id
            or data.get("items_saved") != 5
            or type(data.get(revision)) is not int
            or data[revision] < 1
        ):
            raise EvaluationError("MCP 저장 응답의 평가 건·항목 수·리비전이 다릅니다.")
