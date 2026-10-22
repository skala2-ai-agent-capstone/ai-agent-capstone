"""분류·승인 모듈의 LangGraph: 조회 → 분류 → 기준 생성 → 사람 승인 → 평가 모듈 연결/인계."""

from datetime import datetime, timezone
from typing import Protocol, Self, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import model_validator

from ..classification.agent import ClassificationBackend, classify_work
from ..integrations.mcp_client import MCPError, ToolGateway
from ..integrations.mcp_data import (
    BusinessPacket,
    classification_context,
    fetch_business,
    fetch_policy,
)
from ..rubric.policy import PolicyBundle
from ..shared.models import ClassificationReport, StrictModel, WorkType
from ..shared.workflow_models import (
    Approval,
    CriteriaDraft,
    EvaluationInput,
    EvaluationOutput,
    content_hash,
)
from .approval import WorkflowError, validate_report


class CriteriaBuilder(Protocol):
    async def generate(
        self, work_type: WorkType, policy: PolicyBundle, *,
        previous: CriteriaDraft | None = None, feedback: str | None = None,
    ) -> CriteriaDraft: ...


class MCPHandoff(StrictModel):
    """평가 모듈이 받는 입력. 기존 EvaluationInput과 서버 원본 근거·스냅샷을 함께 전달한다."""
    evaluation_input: EvaluationInput
    business: BusinessPacket
    source_map: dict[str, dict]
    approved_business_sha256: str

    @model_validator(mode="after")
    def check_binding(self) -> Self:
        context, mapping = classification_context(self.business)
        if (self.evaluation_input.run_id != self.business.run_id
                or self.evaluation_input.work_record != context or self.source_map != mapping
                or content_hash(self.business) != self.approved_business_sha256):
            raise ValueError("승인한 업무 스냅샷과 인계 자료가 다릅니다.")
        return self


class Evaluator(Protocol):
    async def evaluate(self, request: MCPHandoff) -> EvaluationOutput: ...


class AgentState(TypedDict, total=False):
    work_id: str
    status: str
    business: dict
    classification: dict
    criteria: dict
    revision: int
    feedback: str | None
    handoff: dict
    report: dict
    issue: str


def build_agent_graph(
    gateway: ToolGateway, classifier: ClassificationBackend, builder: CriteriaBuilder,
    *, evaluator: Evaluator | None = None,
):
    """한 실행에 한 그래프. 토큰/SDK 연결은 closure에만 보관. 체크포인트는 메모리 전용."""
    async def fetch(state):
        try:
            packet = await fetch_business(gateway, state["work_id"])
        except MCPError as exc:
            if exc.code not in {"NOT_FOUND", "TARGET_NOT_FOUND", "EMPTY_TARGET"}:
                raise
            return {"status": "data_hold", "issue": str(exc)}
        return {"business": packet.model_dump(mode="json"), "status": "fetched"}

    async def classify(state):
        context, _ = classification_context(BusinessPacket.model_validate(state["business"]))
        report = await classify_work(context, classifier)
        return {
            "classification": report.model_dump(mode="json"),
            "status": "classified" if report.classification.status == "classified"
            else "classification_hold",
        }

    async def enrich(state):
        packet = BusinessPacket.model_validate(state["business"])
        work_type = state["classification"]["classification"]["work_type"]
        if work_type == "quotation":
            for key, tool, args in (
                ("quote", "compute_reference_quote", {
                    "work_id": state["work_id"],
                    "artifact_version": packet.snapshot["artifact_ref"]["version"],
                }),
                ("approval_requests", "get_approval_requests", {"work_id": state["work_id"]}),
            ):
                reply = await gateway.call("business", tool, args)
                if (reply["meta"]["run_id"] != packet.run_id
                        or reply["meta"]["snapshot"] != packet.snapshot):
                    raise MCPError("SNAPSHOT_CHANGED", tool)
                packet.supplemental[key] = reply["data"]
        return {"business": packet.model_dump(mode="json")}

    async def prepare(state):
        work_type = state["classification"]["classification"]["work_type"]
        try:
            policy = await fetch_policy(gateway, work_type)
        except MCPError as exc:
            if exc.code not in {"NOT_FOUND", "POLICY_INCOMPLETE"}:
                raise
            return {"status": "data_hold", "issue": str(exc)}
        previous = (CriteriaDraft.model_validate(state["criteria"])
                    if state.get("criteria") else None)
        if previous:
            old_versions = {(r.document_id, r.version) for r in previous.references}
            if old_versions != {(c.doc_id, c.version) for c in policy.chunks}:
                raise WorkflowError("내규 버전이 바뀌었습니다. 새 실행으로 다시 확인하세요.")
        draft = await builder.generate(
            work_type, policy, previous=previous, feedback=state.get("feedback"),
        )
        draft = CriteriaDraft.model_validate(draft.model_dump())
        policy_sources = {(c.doc_id, c.version, c.chunk_id): c.text for c in policy.chunks}
        if draft.work_type != work_type or any(
            policy_sources.get((r.document_id, r.version, r.section_id)) != r.content
            for r in draft.references
        ):
            raise WorkflowError("생성된 기준의 유형·근거가 현재 RAG 응답과 다릅니다.")
        return {"criteria": draft.model_dump(mode="json"),
                "revision": state.get("revision", 0) + 1, "status": "awaiting_approval"}

    def approval(state):
        # interrupt 재개 시 이 노드는 처음부터 실행된다. 앞부분에는 외부 호출/변경이 없다.
        draft = CriteriaDraft.model_validate(state["criteria"])
        packet = BusinessPacket.model_validate(state["business"])
        draft_hash, packet_hash = content_hash(draft), content_hash(packet)
        prompt = {
            "kind": "criteria_approval", "run_id": packet.run_id,
            "classification": state["classification"]["classification"],
            "criteria": state["criteria"], "revision": state["revision"],
            "warnings": [warning.model_dump(mode="json") for warning in draft.warnings],
            "criteria_sha256": draft_hash, "business_sha256": packet_hash,
            "actions": ["approve", "revise", "cancel"],
        }
        while True:
            decision = interrupt(prompt)
            if not isinstance(decision, dict):
                prompt = {**prompt, "error": "action과 표시된 초안 해시가 필요합니다."}
                continue
            action = decision.get("action")
            if action == "cancel":
                return {"status": "cancelled"}
            if (decision.get("criteria_sha256") != draft_hash
                    or decision.get("business_sha256") != packet_hash
                    or decision.get("revision") != state["revision"]):
                prompt = {**prompt, "error": "이전 초안의 응답입니다. 현재 초안을 확인하세요."}
                continue
            if action == "revise" and not (
                isinstance(decision.get("feedback"), str) and decision["feedback"].strip()
            ):
                prompt = {**prompt, "error": "수정 요청 내용이 필요합니다."}
                continue
            if action in {"approve", "revise"}:
                break
            prompt = {**prompt, "error": "approve, revise, cancel 중 하나가 필요합니다."}
        if action == "revise":
            return {"status": "revising", "feedback": decision["feedback"].strip()}
        context, mapping = classification_context(packet)
        report = ClassificationReport.model_validate(state["classification"])
        if report.input_sha256 != content_hash(context):
            raise WorkflowError("분류 이후 업무 기록이 바뀌었습니다.")
        request = EvaluationInput(
            run_id=packet.run_id, work_record=context, classification=report.classification,
            approved_criteria=draft, approval=Approval(
                status="approved", run_id=packet.run_id, work_sha256=content_hash(context),
                criteria_sha256=draft_hash, revision=state["revision"],
                approved_at=datetime.now(timezone.utc),
            ),
        )
        handoff = MCPHandoff(
            evaluation_input=request, business=packet, source_map=mapping,
            approved_business_sha256=packet_hash,
        )
        return {"handoff": handoff.model_dump(mode="json"), "status": "approved"}

    async def evaluate(state):
        if evaluator is None:
            return {"status": "ready_for_evaluation"}
        handoff = MCPHandoff.model_validate(state["handoff"])
        result = await evaluator.evaluate(handoff.model_copy(deep=True))
        result = EvaluationOutput.model_validate(result.model_dump())
        validate_report(result, handoff.evaluation_input,
                        additional_sources=handoff.business.evidence_sources())
        return {"status": "completed", "report": result.model_dump(mode="json")}

    graph = StateGraph(AgentState)
    for name, node in (("fetch_work_case", fetch), ("classify_work", classify),
                       ("enrich_context", enrich), ("prepare_criteria", prepare),
                       ("approve_criteria", approval), ("evaluate_or_handoff", evaluate)):
        graph.add_node(name, node)
    graph.add_edge(START, "fetch_work_case")
    graph.add_conditional_edges("fetch_work_case", lambda s:
                                "classify_work" if s["status"] == "fetched" else END)
    graph.add_conditional_edges("classify_work", lambda s:
                                "enrich_context" if s["status"] == "classified" else END)
    graph.add_edge("enrich_context", "prepare_criteria")
    graph.add_conditional_edges("prepare_criteria", lambda s:
                                "approve_criteria" if s["status"] == "awaiting_approval" else END)
    graph.add_conditional_edges("approve_criteria", lambda s: {
        "approved": "evaluate_or_handoff", "revising": "prepare_criteria", "cancelled": END,
    }[s["status"]])
    graph.add_edge("evaluate_or_handoff", END)
    return graph.compile(checkpointer=InMemorySaver())
