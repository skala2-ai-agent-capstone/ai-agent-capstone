"""기존 분류·승인 그래프를 그대로 사용하고 승인된 입력에 평가를 연결한다."""

import json
from copy import deepcopy

from ...observability import trace_step
from ...observability.tracing import trace_span
from ...orchestration.approval import WorkflowError
from ...orchestration.graph import MCPHandoff, build_agent_graph
from ...shared.workflow_models import EvaluationInput, EvaluationOutput, Reference, content_hash
from .models import EvaluationReceipt


def _validate_report(
    report: EvaluationOutput,
    request: EvaluationInput,
    *,
    additional_sources: dict[str, str] | None = None,
    additional_references: list[Reference] | None = None,
) -> None:
    """구조·인용 일치 검증. 근거의 의미상 타당성 검증은 평가 모듈의 별도 책임입니다."""
    if report.run_id != request.run_id or report.criteria_sha256 != content_hash(
        request.approved_criteria
    ):
        raise WorkflowError("평가 결과가 현재 실행 또는 승인한 기준과 다릅니다.")
    criteria = {item.id: item for item in request.approved_criteria.items}
    ids = [item.criterion_id for item in report.items]
    if len(ids) != len(set(ids)) or set(ids) != set(criteria):
        raise WorkflowError("평가 항목이 누락·중복되었거나 승인 기준에 없는 항목입니다.")
    sources = dict(additional_sources or {})
    sources.update(request.work_record.evidence_sources())
    retrieved = {
        (ref.document_id, ref.version, ref.section_id): ref.content
        for ref in additional_references or []
    }
    for item in report.items:
        criterion = criteria[item.criterion_id]
        if item.score is not None and item.score not in {s.score for s in criterion.levels}:
            raise WorkflowError("승인된 점수 조건에 없는 점수입니다.")
        for evidence in item.work_evidence:
            if (
                evidence.source_id not in sources
                or evidence.quote not in sources[evidence.source_id]
            ):
                raise WorkflowError("평가의 업무 근거 ID 또는 인용문이 원문과 다릅니다.")
        has_approved_basis = False
        for evidence in item.rule_evidence:
            is_approved = any(
                evidence.key() == approved.key() and evidence.quote in approved.quote
                for approved in criterion.rule_evidence
            )
            has_approved_basis = has_approved_basis or is_approved
            if not is_approved and not (
                evidence.key() in retrieved and evidence.quote in retrieved[evidence.key()]
            ):
                raise WorkflowError("평가의 기준 근거가 해당 승인 항목의 근거와 다릅니다.")
        if not has_approved_basis:
            raise WorkflowError("해당 항목의 승인된 기준 근거가 없습니다.")
    if any(coaching.criterion_id not in criteria for coaching in report.coaching):
        raise WorkflowError("코칭이 승인 기준에 없는 항목을 참조합니다.")


class EvaluationWorkflow:
    """원본 그래프의 승인·수정·취소 흐름을 보존하는 평가 연결부."""

    def __init__(self, graph, evaluator):
        self.graph = graph
        self.evaluator = evaluator
        self.completed = {}

    @trace_step("evaluation_workflow")
    async def ainvoke(self, request, config):
        result = await self.graph.ainvoke(request, config)
        if result.get("status") != "ready_for_evaluation" or self.evaluator is None:
            return result
        handoff = MCPHandoff.model_validate(result["handoff"])
        key = content_hash(handoff)
        if key not in self.completed:
            with trace_span("evaluate_approved_work", inputs=handoff) as span:
                output = await self.evaluator.evaluate(handoff.model_copy(deep=True))
                span.result(output)
            self.completed[key] = self.complete(handoff, output)
        return {**result, **deepcopy(self.completed[key])}

    @staticmethod
    @trace_step("validate_evaluation_receipt")
    def complete(handoff, output):
        sources = handoff.business.evidence_sources()
        references = []
        catalog = {}
        if isinstance(output, EvaluationReceipt):
            receipt = EvaluationReceipt.model_validate(output.model_dump(mode="json"))
            if (
                receipt.approved_business_sha256 != handoff.approved_business_sha256
                or receipt.snapshot != handoff.business.snapshot
            ):
                raise WorkflowError("평가 결과가 승인한 업무 스냅샷과 다릅니다.")
            allowed = {
                (r["doc_id"], r["version"])
                for r in handoff.business.snapshot["reference_refs"]
                + handoff.business.snapshot.get("linked_doc_refs", [])
            }
            approved = {
                (r.document_id, r.version, r.section_id): r.content
                for r in handoff.evaluation_input.approved_criteria.references
            }
            for ref in receipt.additional_references:
                key = (ref.document_id, ref.version, ref.section_id)
                if key in approved:
                    if approved[key] != ref.content:
                        raise WorkflowError("평가 모듈이 전달한 기준 출처가 승인 내용과 다릅니다.")
                elif (ref.document_id, ref.version) not in allowed:
                    raise WorkflowError("스냅샷에서 허용하지 않은 추가 참조 근거입니다.")
            for source_id, text in receipt.additional_sources.items():
                if source_id in sources and sources[source_id] != text:
                    try:
                        old = json.loads(sources[source_id])
                        new = json.loads(text)
                    except (TypeError, ValueError):
                        raise WorkflowError(
                            "승인 단계와 평가 단계의 근거 원문이 서로 다릅니다."
                        ) from None
                    if old != new:
                        raise WorkflowError(
                            "승인 단계와 평가 단계의 구조화된 근거 원문이 서로 다릅니다."
                        )
                sources[source_id] = text
            references = receipt.additional_references
            result = receipt.report
            catalog = receipt.model_dump(mode="json", exclude={"report"})
        else:
            result = EvaluationOutput.model_validate(output.model_dump())
        _validate_report(
            result,
            handoff.evaluation_input,
            additional_sources=sources,
            additional_references=references,
        )
        return {
            "status": "completed",
            "report": result.model_dump(mode="json"),
            "evaluation_catalog": catalog,
        }


def build_evaluation_workflow(gateway, classifier, builder, *, evaluator=None):
    graph = build_agent_graph(gateway, classifier, builder)
    return EvaluationWorkflow(graph, evaluator)
