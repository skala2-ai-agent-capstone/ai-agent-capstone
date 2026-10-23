"""검증된 내부 기준을 MCP save_rubric의 work_type·items 형식으로 변환한다."""

from ..shared.workflow_models import CriteriaDraft
from .builder import (
    ALIASES,
    ITEM_IDS,
    ITEM_NAMES,
    RubricError,
    RubricRow,
    WorkTypeRubric,
    check_policy,
    rubric_issues,
)
from .policy import PolicyBundle

MCP_ALIASES = {canonical: external for external, canonical in ALIASES.items()}


def to_mcp_rubric(draft: CriteriaDraft, policy: PolicyBundle) -> dict[str, object]:
    """출처·점수·항목을 검사해 JSON 객체를 반환한다. 저장 도구는 호출하지 않는다.

    구간 단위의 정확한 근거는 draft에 남기고, related_clauses에는 해당 구간의
    실제 clause_id를 전달한다. 원문·경고·생성 정보는 저장 인자에 포함하지 않는다.
    """
    draft = CriteriaDraft.model_validate(draft.model_dump())
    policy = PolicyBundle.model_validate(policy.model_dump())
    check_policy(draft.work_type, policy)
    expected_ids = [f"{draft.work_type}.{item_id}" for item_id in ITEM_IDS]
    if [item.id for item in draft.items] != expected_ids:
        raise RubricError("평가 항목은 정해진 ID·순서로 중복 없이 5개여야 합니다.")

    chunks = {(c.doc_id, c.version, c.chunk_id): c for c in policy.chunks}
    for ref in draft.references:
        chunk = chunks.get((ref.document_id, ref.version, ref.section_id))
        if chunk is None or ref.content != chunk.text:
            raise RubricError("기준 출처의 문서·버전·구간 또는 원문이 입력 내규와 다릅니다.")

    rows = []
    for name, item in zip(ITEM_NAMES, draft.items, strict=True):
        prefix = f"{name}: "
        if not item.title.startswith(prefix):
            raise RubricError(f"{name}: 항목 이름과 판단 기준의 형식이 다릅니다.")
        scores = {level.score: level.condition for level in item.levels}
        expected_scores = {0, 2} if name == "보안 준수" else {0, 1, 2}
        if set(scores) != expected_scores:
            raise RubricError(f"{name}: 점수 구성은 {sorted(expected_scores)}이어야 합니다.")

        # 문자열을 잘라 상위 조항을 추측하지 않고 입력 내규의 매핑을 사용한다.
        clauses = list(dict.fromkeys(
            chunks[evidence.key()].clause_id for evidence in item.rule_evidence
        ))
        rows.append(RubricRow(
            item=name, criterion=item.title[len(prefix):],
            score_0=scores[0], score_1="해당 없음" if name == "보안 준수" else scores[1],
            score_2=scores[2], unobserved=item.unobserved_condition,
            needs_review=item.review_condition, evidence_sources=item.evidence_sources,
            related_clauses=clauses,
        ))

    rubric = WorkTypeRubric(
        work_type=MCP_ALIASES.get(draft.work_type, draft.work_type), rows=rows,
    )
    issues = rubric_issues(rubric, draft.work_type, policy)
    if issues:
        raise RubricError("; ".join(issues))
    return {"work_type": rubric.work_type, "items": [row.model_dump(mode="json") for row in rows]}
