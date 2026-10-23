"""LLM 평가 결과를 승인된 기준과 실제 조회 원문에 대조한다."""

import re

from ...shared.evidence_models import EvaluationEvidence
from ...shared.workflow_models import EvaluationInput, content_hash
from .contract import (
    ITEM_KEYS,
    MISSING_WORK_MESSAGES,
    eligible_work_sources,
    score_issue,
)
from .materials import EvaluationMaterials
from .models import AgentEvaluationOutput
from .stored_rubric import StoredEvaluationInput, checked_input

VISIBLE_SECRET = re.compile(
    r"01[016789]-?\d{3,4}-?\d{4}|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\bsk-[A-Za-z0-9_-]{20,}\b"
)


class EvaluationError(ValueError):
    pass


def validate_criteria(request: EvaluationInput) -> None:
    criteria = request.approved_criteria
    expected = {f"{criteria.work_type}.{key}" for key in ITEM_KEYS}
    if {item.id for item in criteria.items} != expected or len(criteria.items) != 5:
        raise EvaluationError("승인된 기준은 업무 유형에 맞는 고유한 5개 항목이어야 합니다.")
    for item in criteria.items:
        scores = {level.score for level in item.levels}
        allowed = {0, 2} if item.id.endswith(".security") else {0, 1, 2}
        if not scores or not scores <= allowed:
            raise EvaluationError("승인된 기준의 허용 점수가 잘못되었습니다.")


def evidence_catalog(request, evidence, materials):
    # 저장 기준 경로의 WorkContext는 업무 메타데이터만 담는다. 근거는 MCP 원본 ID만 사용한다.
    sources = (
        {} if isinstance(request, StoredEvaluationInput) else request.work_record.evidence_sources()
    )
    kinds = {key: "context" for key in sources}
    kinds.update({m.id: f"message:{m.role}" for m in request.work_record.messages})
    kinds.update(
        {
            a.id: "reference" if a.stage == "reference" else "artifact:unknown"
            for a in request.work_record.artifacts
        }
    )

    def merge(new_sources, new_kinds):
        for key, text in new_sources.items():
            if key in sources and sources[key] != text:
                raise EvaluationError("동일 근거 ID의 원문이 서로 다릅니다.")
            sources[key] = text
            kinds[key] = new_kinds[key]

    base_sources = evidence.evidence_sources()
    base_kinds = {key: "customer_document" for key in base_sources}
    if evidence.work_case.registration is not None:
        base_kinds[evidence.work_case.registration.evidence_id] = "registration"
    for artifact in evidence.artifacts:
        kind = (
            "target_artifact"
            if artifact.is_evaluation_target and artifact.author == "employee"
            else "artifact:reference"
        )
        base_kinds.update({span.span_id: kind for span in artifact.spans})
    if evidence.policy_check is not None:
        base_kinds[evidence.policy_check.evidence_id] = "policy_check"
    merge(base_sources, base_kinds)
    if materials.work_sources.keys() != materials.source_kinds.keys():
        raise EvaluationError("추가 근거의 ID와 종류 목록이 다릅니다.")
    merge(materials.work_sources, materials.source_kinds)
    return sources, kinds


def validate_materials(request, evidence, materials) -> None:
    if (
        request.run_id != evidence.context.run_id
        or request.work_record.work_id != evidence.context.work_id
        or content_hash(evidence.context) != content_hash(materials.context)
        or content_hash(evidence) != materials.evidence_sha256
    ):
        raise EvaluationError("승인 입력과 추가 근거의 실행·스냅샷·내용이 일치하지 않습니다.")
    approved = {
        (r.document_id, r.version, r.section_id): r.content
        for r in request.approved_criteria.references
    }
    allowed = {
        (r["doc_id"], r["version"])
        for r in evidence.context.snapshot["reference_refs"]
        + evidence.context.snapshot["linked_doc_refs"]
    }
    for ref in materials.references:
        key = (ref.document_id, ref.version, ref.section_id)
        if key in approved:
            if ref.content != approved[key]:
                raise EvaluationError("승인된 기준 출처의 내용이 변경되었습니다.")
        elif (ref.document_id, ref.version) not in allowed:
            raise EvaluationError("허용된 문서 버전 목록에 없는 참조 근거입니다.")


def reference_catalog(request, materials):
    references = {}
    for ref in request.approved_criteria.references + materials.references:
        key = (ref.document_id, ref.version, ref.section_id)
        if key in references and references[key].content != ref.content:
            raise EvaluationError("같은 문서 버전·구간의 기준 원문이 서로 다릅니다.")
        references[key] = ref
    return references


def validate_evaluation_result(
    request: EvaluationInput,
    result: AgentEvaluationOutput,
    *,
    evidence: EvaluationEvidence,
    materials: EvaluationMaterials,
) -> AgentEvaluationOutput:
    request = checked_input(request)
    evidence = EvaluationEvidence.model_validate(evidence.model_dump(mode="json"))
    materials = EvaluationMaterials.model_validate(materials.model_dump(mode="json"))
    result = AgentEvaluationOutput.model_validate(result.model_dump(mode="json"))
    validate_criteria(request)
    validate_materials(request, evidence, materials)
    if (
        result.run_id != request.run_id
        or result.criteria_sha256 != content_hash(request.approved_criteria)
    ):
        raise EvaluationError("평가 결과의 실행 또는 기준 해시가 다릅니다.")
    criteria = {item.id: item for item in request.approved_criteria.items}
    if {item.criterion_id for item in result.items} != criteria.keys():
        raise EvaluationError("평가 항목이 승인된 기준과 다릅니다.")
    sources, kinds = evidence_catalog(request, evidence, materials)
    references = {key: ref.content for key, ref in reference_catalog(request, materials).items()}
    for item in result.items:
        criterion = criteria[item.criterion_id]
        if (isinstance(request, StoredEvaluationInput) and item.review_required
                and item.observation not in {"needs_review", "insufficient_data"}):
            raise EvaluationError(
                "저장 평가의 검토 필요 항목은 needs_review와 score=null로 반환해야 합니다."
            )
        if item.score is not None and item.score not in {level.score for level in criterion.levels}:
            raise EvaluationError("승인된 기준에서 허용하지 않는 점수입니다.")
        work_ids = set()
        for proof in item.work_evidence:
            if proof.source_id not in sources:
                raise EvaluationError(
                    f"{item.criterion_id}: 업무 근거 ID가 실제 조회 목록에 없습니다."
                )
            if proof.quote not in sources[proof.source_id]:
                raise EvaluationError(
                    f"{item.criterion_id}: 선택한 업무 근거의 인용문이 원문과 다릅니다."
                )
            work_ids.add(proof.source_id)
        for proof in item.rule_evidence:
            if proof.key() not in references or proof.quote not in references[proof.key()]:
                raise EvaluationError("기준 근거의 문서·버전·구간 또는 인용문이 다릅니다.")
        if not any(
            proof.key() == approved.key() and proof.quote in approved.quote
            for proof in item.rule_evidence for approved in criterion.rule_evidence
        ):
            raise EvaluationError("해당 항목의 승인된 기준 근거가 없습니다.")
        if item.observation != "observed":
            continue
        key = item.criterion_id.rsplit(".", 1)[1]
        eligible = eligible_work_sources(
            key, kinds, request.work_record.work_id,
            policy_available=evidence.policy_check is not None,
        )
        if not work_ids & eligible:
            raise EvaluationError(MISSING_WORK_MESSAGES[key])
        issue = score_issue(
            key, item.score, request.classification.work_type,
            policy_violations=bool(evidence.policy_check and evidence.policy_check.violations),
            reference_quote_available=f"{request.work_record.work_id}:reference_quote" in sources,
        )
        if issue:
            raise EvaluationError(issue)
    coaching_ids = [entry.criterion_id for entry in result.coaching]
    if len(coaching_ids) != 5 or set(coaching_ids) != criteria.keys():
        raise EvaluationError("코칭은 평가 항목별로 중복 없이 하나씩 필요합니다.")
    if any(
        VISIBLE_SECRET.search(text)
        for text in (
            [item.reason for item in result.items] + [entry.action for entry in result.coaching]
        )
    ):
        raise EvaluationError("판단 이유·코칭에 마스킹되지 않은 연락처 또는 인증정보가 있습니다.")
    # 개선율·ROI 등 입력에 없는 성과 수치를 요약에서 만들어내지 않는다.
    scored = [item for item in result.items if item.score is not None]
    total = sum(item.score for item in scored)
    reviews = sum(item.review_required for item in result.items)
    result.summary = (
        f"5개 항목 중 {len(scored)}개 채점, {5 - len(scored)}개 점수 없음. "
        + (f"관측 항목 합계 {total}/{2 * len(scored)}점. " if scored else "")
        + f"검토 표시 {reviews}개."
    )
    return result
