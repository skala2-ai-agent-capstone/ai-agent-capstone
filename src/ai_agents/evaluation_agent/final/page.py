"""검증된 최종 평가를 화면 데이터로 투영한다. LLM 재호출·점수 재평가 없음."""

import hashlib
import json

from ...shared.final_page_contracts import (
    ITEM_NAMES,
    OBSERVATION_LABELS,
    FinalEvaluationPage,
    FinalReportSaveRequest,
    PageItem,
    PageRubric,
    PageWork,
    score_for,
)
from ...shared.workflow_models import content_hash
from .validation import EvaluationError, evidence_catalog, validate_evaluation_result

WORK_LABELS = {
    "rfp_analysis": "RFP 요구사항 분석", "proposal": "고객 맞춤 제안서 작성",
    "quote": "견적서 작성", "inquiry_reply": "고객 질의 답변",
}


def build_final_page(prepared) -> FinalEvaluationPage:
    request, evidence, materials = prepared.request, prepared.evidence, prepared.materials
    report = validate_evaluation_result(
        request, prepared.report, evidence=evidence, materials=materials,
    )
    rubric = request.stored_rubric
    original_ids = rubric.item_ids()
    evaluations = {original_ids[item.criterion_id]: item for item in report.items}
    criteria = {original_ids[item.id]: item for item in request.approved_criteria.items}
    coaching = {original_ids[item.criterion_id]: item.action for item in report.coaching}
    _, kinds = evidence_catalog(request, evidence, materials)
    rows = {item.item: item for item in rubric.items}
    items = []
    for title in ITEM_NAMES:
        row = rows[title]
        result, criterion = evaluations[row.item_id], criteria[row.item_id]
        items.append(PageItem(
            item_id=row.item_id, title=title, criterion=row.criterion,
            score_levels=criterion.levels, unobserved_condition=row.unobserved,
            review_condition=row.needs_review or "", observation=result.observation,
            status_label=OBSERVATION_LABELS[result.observation], score=result.score,
            included_in_total=result.observation == "observed", reason=result.reason,
            review_required=(result.review_required or result.observation in {
                "needs_review", "insufficient_data",
            }),
            work_evidence=[
                {**proof.model_dump(), "source_kind": kinds[proof.source_id]}
                for proof in result.work_evidence
            ],
            rule_evidence=[proof.model_dump() for proof in result.rule_evidence],
            coaching=coaching[row.item_id],
        ))
    snapshot = evidence.context.snapshot
    score = score_for(items)
    reviews = sum(item.review_required for item in items)
    summary = (
        f"5개 항목 중 {score.scored_count}개 채점, {score.excluded_count}개 점수 없음. "
        + (f"관측 항목 합계 {score.display}점. " if score.scored_count else "합계 산정 불가. ")
        + f"검토 필요 {reviews}개."
    )
    return FinalEvaluationPage(
        evaluation_id=prepared.job.evaluation_id, operation_id=prepared.job.operation_id,
        source_run_id=request.run_id,
        snapshot_sha256=hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
        work=PageWork(
            work_id=request.work_record.work_id,
            title=request.work_record.title or request.work_record.work_id,
            work_type=rubric.work_type, work_type_label=WORK_LABELS[rubric.work_type],
            artifact_id=evidence.context.target.artifact_id,
            artifact_version=evidence.context.target.version,
        ),
        rubric=PageRubric(
            doc_id=rubric.doc_id, version=rubric.version, criteria_sha256=report.criteria_sha256,
        ),
        score=score, summary=summary,
        review_required=any(item.review_required for item in items), items=items,
    )


def build_report_save_request(prepared) -> FinalReportSaveRequest:
    """정본을 재검증하고 두 저장 리비전에 연결한다. 로컬 캐시 page_result는 신뢰하지 않는다."""
    page = build_final_page(prepared)
    revisions = {}
    for tool, field in (
        ("save_evaluation_draft", "draft_revision"),
        ("save_coaching", "coaching_revision"),
    ):
        receipt = prepared.receipts.get(tool, {})
        if (receipt.get("evaluation_id") != prepared.job.evaluation_id
                or type(receipt.get("items_saved")) is not int or receipt["items_saved"] != 5
                or type(receipt.get(field)) is not int or receipt[field] < 1):
            raise EvaluationError("페이지 저장 인자는 초안·코칭 저장 확인 후 생성할 수 있습니다.")
        revisions[field] = receipt[field]
    return FinalReportSaveRequest(
        evaluation_id=page.evaluation_id, operation_id=page.operation_id,
        report=page, report_sha256=content_hash(page),
        idempotency_key=f"final:{prepared.job.key()}:report:v1", **revisions,
    )
