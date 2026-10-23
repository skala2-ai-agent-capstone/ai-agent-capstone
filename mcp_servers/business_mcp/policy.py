"""마스킹 전 원문으로 하는 정책 위반 탐지 (명세 4.2).

위반 규칙은 직원 행동에만 적용한다: role=user 메시지(스냅샷 대화 범위)와 직원이 저장한 평가 대상 산출물.
고객 원문(source_documents, source_message)의 개인정보는 위반이 아니라 마스킹 집계에만 들어간다.
응답에는 위치(메시지 ID·구간 ID)만 넣고 매칭된 원문은 넣지 않는다.
"""
from __future__ import annotations

import re

from .store import Store

PHONE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
INTERNAL_PRICING = [re.compile(r"최대\s?\d+%까지"), re.compile(r"팀장\s?승인"), re.compile(r"임원\s?승인"), re.compile(r"가격심의위원회")]
ROADMAP_PHRASE = re.compile(r"출시\s?(될\s?)?예정")
TOKEN_SPLIT = re.compile(r"[\s,()·]+")


def _clauses(content: str) -> list[list[str]]:
    out = []
    for part in re.split(r",\s+|\n", content):
        toks = [t for t in TOKEN_SPLIT.split(part) if t]
        if len(toks) >= 3:
            out.append(toks)
    return out


def _clause_hit(text: str, toks: list[str]) -> bool:
    """기밀 문서의 한 구절과 내용이 일치하는지: 숫자가 든 토큰은 모두, 전체 토큰은 80% 이상 본문에 있어야 한다."""
    present = [t in text for t in toks]
    digits_ok = all(p for t, p in zip(toks, present) if re.search(r"\d", t))
    return digits_ok and sum(present) / len(toks) >= 0.8


class PolicyChecker:
    def __init__(self, store: Store):
        self.store = store
        conf = [d for d in store.documents if not d.get("indexed")]
        self.confidential = [(d["doc_id"], _clauses(d.get("content", ""))) for d in conf]
        self.roadmap = [(i, c) for i, c in self.confidential if i.upper().startswith("ROADMAP")]

    def _conf_hit(self, text: str, docs) -> bool:
        for doc_id, clauses in docs:
            if doc_id in text or any(_clause_hit(text, c) for c in clauses):
                return True
        return False

    def check(self, claims: dict) -> dict:
        s = self.store
        wid = claims["work_id"]
        raw = s.raw[wid]
        snap = claims["snapshot"]
        rng = snap["conversation_range"]
        version = snap["artifact_ref"]["version"]
        msgs = [m for m in raw["messages"] if rng["from_seq"] <= m["seq"] <= rng["to_seq"]]
        av = s.version_rec(wid, version)
        violations: list[dict] = []
        seen = set()

        def add(rule_id, clause, location, **kw):
            if (rule_id, location) in seen:
                return
            seen.add((rule_id, location))
            violations.append({"rule_id": rule_id, "clause": clause, "location": location, **kw})

        for m in msgs:
            if m["role"] != "user":
                continue
            if PHONE.search(m["content"]) or EMAIL.search(m["content"]):
                add("V-PII-INPUT", "ART-07", m["message_id"])
            if self._conf_hit(m["content"], self.confidential):
                add("V-CONFIDENTIAL-INPUT", "ART-06", m["message_id"])

        if av["author"] == "employee":
            own = raw["work_case"]["customer_id"]
            # 고객 미지정 업무는 어느 고객이 "타 고객"인지 알 수 없어 타 고객 노출 검사를 하지 않는다
            others = [] if own is None else [
                (c["customer_id"], re.sub(r"\(가상\)$", "", c["name"]).strip())
                for c in s.customers.values()
                if c["customer_id"] != own and not c.get("approved_reference")
            ]
            spans = av["spans"] or [{"span_id": f"{av['artifact_id']}:{version}", "text": av["content"]}]
            for sp in spans:
                t = sp["text"]
                for cid, name in others:
                    if name and name in t:
                        add("V-OTHER-CUSTOMER", "ART-09", sp["span_id"], matched_customer_id=cid)
                if any(p.search(t) for p in INTERNAL_PRICING):
                    add("V-INTERNAL-PRICING", "ART-20", sp["span_id"])
                if ROADMAP_PHRASE.search(t) or self._conf_hit(t, self.roadmap):
                    add("V-ROADMAP", "ART-09", sp["span_id"])

        # 마스킹 집계 (원문 기준, 스냅샷 범위)
        counts: dict[tuple[str, str], int] = {}

        def tally(source, text):
            for rid, n in s.masker.count(text).items():
                counts[(rid, source)] = counts.get((rid, source), 0) + n

        for d in s.visible_source_docs(claims, masked=False):
            tally("source_documents", d["content"])
        if raw["work_case"].get("source_message"):
            tally("source_message", raw["work_case"]["source_message"]["content"])
        for m in msgs:
            tally("messages", m["content"])
        for v in raw["artifact_versions"]:
            if v["version"] in s.allowed_versions(claims):
                tally("artifacts", v["content"])

        return {
            "work_id": wid,
            "version": version,
            "evidence_id": f"{wid}:policy_check",
            "checked_at": s.completed_at(wid, version),
            "scope": {
                "user_message_ids": [m["message_id"] for m in msgs if m["role"] == "user"],
                "artifact_ref": {"artifact_id": av["artifact_id"], "version": version, "author": av["author"]},
                "note": "위반 규칙은 직원 입력과 직원이 저장한 산출물에만 적용. 고객 원문은 마스킹 대상",
            },
            "violations": violations,
            "masking": [{"rule_id": r, "source": src, "count": n} for (r, src), n in sorted(counts.items())],
        }
