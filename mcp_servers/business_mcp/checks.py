"""저장 내용 검증: 평가 항목·코칭·자유 텍스트.

최종 평가(writes)와 중간 점검(live)이 같은 규칙을 쓴다. 다른 것은 근거로 쓸 수 있는 ID 범위(pool)와
그 범위를 벗어났을 때의 오류 문구뿐이다(최종 평가는 스냅샷, 중간 점검은 최근 메시지 5개).
"""
from __future__ import annotations

from typing import Callable

from common.runtime import Masker, validation

ITEM_STATUS = {"채점", "미관측", "검토 필요"}
COACH_KINDS = {"강점", "보완점", "기록 안내"}
SECURITY_ITEM = "보안 준수"     # 0 또는 2점만

ItemRule = Callable[[str, dict], str | None]   # (항목명, 항목) → 오류 문구. 호출부 전용 규칙


def check_text(masker: Masker, text: str | None, where: str) -> None:
    hits = masker.has_sensitive(text or "")
    if hits:
        raise validation(f"{where}에 민감정보 패턴({', '.join(hits)})이 있습니다. 원문 대신 위치나 대체 표기를 쓰세요")


def check_items(items: list, rubric_items: list[dict], pool: set[str], masker: Masker, *, outside: str,
                rule: ItemRule | None = None) -> list[dict]:
    """루브릭 항목 전부에 대한 결과인지 검증하고 저장할 모양으로 돌려준다.
    채점: score(보안 준수는 0·2)와 evidence_ids 1개 이상 / 미관측: score=null / 검토 필요: score=null + reason.
    outside: 근거 ID가 pool 밖일 때 오류 문구."""
    by_id = {i["item_id"]: i for i in rubric_items}
    if not isinstance(items, list) or len(items) != len(by_id):
        raise validation(f"items는 루브릭 항목 {len(by_id)}개와 같은 수여야 합니다")
    ids = [i.get("item_id") for i in items if isinstance(i, dict)]
    if sorted(ids) != sorted(by_id):
        raise validation(f"item_id가 루브릭 항목과 다릅니다. 필요: {sorted(by_id)}")
    errors = []
    for it in items:
        rid = it["item_id"]
        name = by_id[rid]["item"]
        if it.get("item") not in (None, name):
            errors.append(f"{rid}: item 이름은 '{name}'")
        st, score, reason = it.get("status"), it.get("score"), it.get("reason")
        ev_ids = it.get("evidence_ids") or []
        if st not in ITEM_STATUS:
            errors.append(f"{rid}: status는 채점·미관측·검토 필요 중 하나")
            continue
        if rule and (msg := rule(name, it)):
            errors.append(f"{rid}: {msg}")
        if st == "채점":
            allowed = {0, 2} if name == SECURITY_ITEM else {0, 1, 2}
            if not isinstance(score, int) or isinstance(score, bool) or score not in allowed:
                errors.append(f"{rid}: score는 {sorted(allowed)} 중 하나")
            if len(ev_ids) < 1:
                errors.append(f"{rid}: 채점 항목은 evidence_ids가 1개 이상 필요")
        elif score is not None:
            errors.append(f"{rid}: {st} 항목은 score=null")
        if st == "검토 필요" and not reason:
            errors.append(f"{rid}: 검토 필요 항목은 reason 필수")
        unknown = [e for e in ev_ids if e not in pool]
        if unknown:
            errors.append(f"{rid}: {outside} {unknown}")
        if masker.has_sensitive(reason or ""):
            errors.append(f"{rid}: reason에 민감정보 패턴")
    if errors:
        raise validation("; ".join(errors))
    return [{"item_id": i["item_id"], "item": by_id[i["item_id"]]["item"], "status": i["status"], "score": i.get("score"),
             "reason": i.get("reason"), "evidence_ids": i.get("evidence_ids") or []} for i in items]


def check_coaching(coaching: list, saved: set[str], pool: set[str], masker: Masker, *, not_saved: str,
                   outside: str) -> list[dict]:
    """코칭 {item_id, kind, target_span_id, text}. item_id는 저장된 항목, target_span_id는 pool 안(기록 안내는 생략 가능).
    not_saved·outside: item_id·target_span_id가 범위를 벗어났을 때의 오류 문구."""
    if not isinstance(coaching, list):
        raise validation("coaching은 목록이어야 합니다")
    errors = []
    for n, x in enumerate(coaching, 1):
        if not isinstance(x, dict):
            errors.append(f"#{n}: 객체가 아님")
            continue
        if x.get("item_id") not in saved:
            errors.append(f"#{n}: item_id {x.get('item_id')}는 {not_saved}")
        kind, tgt = x.get("kind"), x.get("target_span_id")
        if kind not in COACH_KINDS:
            errors.append(f"#{n}: kind는 강점·보완점·기록 안내 중 하나")
        if kind != "기록 안내" and not tgt:
            errors.append(f"#{n}: target_span_id 필요 (기록 안내만 생략 가능)")
        if tgt and tgt not in pool:
            errors.append(f"#{n}: target_span_id {tgt}가 {outside}")
        if not x.get("text"):
            errors.append(f"#{n}: text 필요")
        if masker.has_sensitive(x.get("text") or ""):
            errors.append(f"#{n}: text에 민감정보 패턴")
    if errors:
        raise validation("; ".join(errors))
    return [{k: x.get(k) for k in ("item_id", "kind", "target_span_id", "text")} for x in coaching]
