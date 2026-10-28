"""최종 평가 결과를 검증된 근거 ID로 MCP 저장 계약에 맞춘다."""

import hashlib
import json

from .contract import eligible_work_sources
from .validation import evidence_catalog, validate_evaluation_result


def save_arguments(prepared):
    request, report = prepared.request, prepared.report
    validate_evaluation_result(
        request,
        report,
        evidence=prepared.evidence,
        materials=prepared.materials,
    )
    ids = request.stored_rubric.item_ids()
    titles = {i.item_id: i.item for i in request.stored_rubric.items}
    states = {
        "observed": "채점",
        "unobserved": "미관측",
        "insufficient_data": "검토 필요",
        "needs_review": "검토 필요",
    }
    items, coaching = [], []
    actions = {c.criterion_id: c.action for c in report.coaching}
    _, kinds = evidence_catalog(request, prepared.evidence, prepared.materials)
    for item in report.items:
        item_id = ids[item.criterion_id]
        evidence_ids = list(dict.fromkeys(e.source_id for e in item.work_evidence))
        items.append(
            {
                "item_id": item_id,
                "item": titles[item_id],
                "status": states[item.observation],
                "score": item.score,
                "reason": item.reason,
                "evidence_ids": evidence_ids,
            }
        )
        eligible = eligible_work_sources(
            item.criterion_id.rsplit(".", 1)[1],
            kinds,
            request.work_record.work_id,
            policy_available=prepared.evidence.policy_check is not None,
        )
        # 채점된 항목만 근거 구간을 코칭 대상으로 삼는다. 검토 필요·자료 부족·미관측은 기록 안내다.
        target = next((eid for eid in evidence_ids if eid in eligible), None) \
            if item.observation == "observed" else None
        # 점수만으로 코칭을 '강점'이라고 추정하지 않는다. 기존 모델 코칭은 개선 행동이다.
        coaching.append(
            {
                "item_id": item_id,
                "kind": "보완점" if target else "기록 안내",
                "target_span_id": target,
                "text": actions[item.criterion_id],
            }
        )
    common = {"evaluation_id": prepared.job.evaluation_id}
    key = prepared.job.key()
    draft_args = {**common, "rubric_version": request.stored_rubric.version, "items": items}
    coaching_args = {**common, "coaching": coaching}
    # 같은 체크포인트로 다시 저장하면 같은 키라 이전 응답을 돌려받는다. 체크포인트를 잃고
    # 다시 평가해 내용이 달라지면 키도 달라져, 이전 초안에 새 코칭이 붙는 대신 둘 다 새 판이 된다.
    return {
        "save_evaluation_draft": {
            **draft_args, "idempotency_key": f"final:{key}:draft:{_digest(draft_args)}",
        },
        "save_coaching": {
            **coaching_args, "idempotency_key": f"final:{key}:coaching:{_digest(coaching_args)}",
        },
    }


def _digest(arguments):
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
