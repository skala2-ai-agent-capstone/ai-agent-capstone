"""업무 시스템 데이터 조회 (명세 1·3장). 저장소는 PostgreSQL (business_mcp/pg.py).

원문과 마스킹본은 적재할 때 테이블에 같이 들어간다 (7장 기본안 2). 정책 검사는 원문으로,
모든 도구 응답은 마스킹본으로 만든다. 업무 데이터는 도구 호출마다 DB에서 읽고, 한 호출 안에서만 캐시한다.
기준 데이터(조직·고객·가격·정책 규칙·문서)는 시작할 때 읽고 /internal/reload로 다시 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from difflib import SequenceMatcher

from common.runtime import Masker, eval_id_for, forbidden, not_found, validation, version_conflict  # noqa: F401

from .pg import Repo

LOC_RE = re.compile(r"^\s*(\d+(?:\.\d+)*\s*\(p\.\s*\d+\))")
LINE_KEY_RE = re.compile(r"^L\d+$")


def span_key(span_id: str) -> str:
    return span_id.rsplit(":", 1)[1]


class _WorkMap:
    """work_id → 케이스 raw 모양 dict. 도구 호출 단위로 캐시한다 (Store.begin에서 비움)."""

    def __init__(self, store: "Store", masked: bool):
        self.store, self.masked = store, masked

    def __getitem__(self, work_id: str) -> dict:
        cache = self.store._cache()
        key = (work_id, self.masked)
        if key not in cache:
            cache[key] = self.store.repo.work(work_id, self.masked)
        if cache[key] is None:
            raise KeyError(work_id)
        return cache[key]

    def __contains__(self, work_id: str) -> bool:
        try:
            self[work_id]
            return True
        except KeyError:
            return False


class _Lookup:
    def __init__(self, fn):
        self.fn = fn

    def get(self, key, default=None):
        v = self.fn(key)
        return default if v is None else v


class Store:
    def __init__(self, repo: Repo, masker: Masker):
        self.repo = repo
        self.masker = masker
        self._local = threading.local()
        self.raw = _WorkMap(self, masked=False)
        self.masked = _WorkMap(self, masked=True)
        self.artifact_owner = _Lookup(repo.artifact_owner)
        self.attachment_owner = _Lookup(repo.attachment_owner)
        self.case_of = _Lookup(repo.case_id)
        self.reload()

    def reload(self) -> None:
        r = self.repo
        self.org = r.organization()
        self.customers = {c["customer_id"]: c for c in r.customers()}
        self.pricing = r.pricing()
        self.policy_rules = r.policy_rules()
        self.documents = r.documents()
        self.historical = {w["work_id"]: w for w in r.historical()}
        self.users = {u["user_id"]: u for u in self.org["users"]}
        self.docs = {(d["doc_id"], d["version"]): d for d in self.documents}
        self.rubrics = {(d["work_type"], d["version"]): d for d in self.documents if d.get("collection") == "rubrics"}
        self.fault_plans = r.fault_plans("business-mcp")
        # 응답 마스킹에 쓴 규칙의 버전 (중간 피드백 get_messages의 masking_version). 규칙이 바뀌면 값도 바뀐다
        masking = [x for x in self.policy_rules["rules"] if x.get("kind") == "masking"]
        digest = hashlib.sha256(json.dumps(masking, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.masking_version = f"mask-{digest[:12]}"

    def rubric(self, work_type: str, version: str) -> dict:
        r = self.rubrics.get((work_type, version))
        if not r:
            raise not_found(f"루브릭 RUB-{work_type} {version}")
        return r

    def _cache(self) -> dict:
        if not hasattr(self._local, "cache"):
            self._local.cache = {}
        return self._local.cache

    def begin(self) -> None:
        """도구 호출 시작: 이전 호출의 업무 캐시를 버린다."""
        self._local.cache = {}

    def version_rec(self, work_id: str, version: str, masked: bool = False) -> dict:
        src = self.masked if masked else self.raw
        for v in src[work_id]["artifact_versions"]:
            if v["version"] == version:
                return v
        raise not_found(f"산출물 버전 {version}")

    def build_snapshot(self, work_id: str, version: str) -> dict:
        raw = self.raw[work_id]
        av = self.version_rec(work_id, version)
        cutoff = av["saved_at"]
        seqs = [m["seq"] for m in raw["messages"] if m["created_at"] <= cutoff]
        linked_doc_refs = []
        for lw in raw["work_case"]["linked_work_ids"]:
            h = self.historical.get(lw)
            if h and h.get("final_artifact", {}).get("doc_id"):
                linked_doc_refs.append({"doc_id": h["final_artifact"]["doc_id"], "version": h["final_artifact"]["version"]})
        return {
            "artifact_ref": {"artifact_id": av["artifact_id"], "version": version, "saved_at": cutoff},
            "reference_refs": [{"doc_id": r["doc_id"], "version": r["version"]} for r in raw["attached_references"]],
            "conversation_range": {"from_seq": min(seqs) if seqs else 0, "to_seq": max(seqs) if seqs else 0},
            "linked_doc_refs": linked_doc_refs,
        }

    def completed_at(self, work_id: str, version: str) -> str | None:
        for e in self.raw[work_id]["events"]:
            if e["event_type"] == "work.completed" and e.get("artifact_version") == version:
                return e["occurred_at"]
        return self.raw[work_id]["work_case"].get("completed_at")

    # ── 범위 확인 ──
    def own_work(self, claims: dict, work_id: str) -> str:
        if work_id != claims["work_id"]:
            raise forbidden(f"{work_id}는 이 실행의 평가 대상 업무가 아닙니다")
        if work_id not in self.raw or self.raw[work_id]["work_case"]["company_id"] != claims["company_id"]:
            raise forbidden()
        return work_id

    def cutoff(self, claims: dict) -> str:
        return claims["snapshot"]["artifact_ref"]["saved_at"]

    def allowed_versions(self, claims: dict) -> list[str]:
        cut = self.cutoff(claims)
        return [v["version"] for v in self.raw[claims["work_id"]]["artifact_versions"] if v["saved_at"] <= cut]

    def linked_case_works(self, claims: dict) -> list[str]:
        wc = self.raw[claims["work_id"]]["work_case"]
        return [w for w in wc["linked_work_ids"] if w in self.raw and self.raw[w]["work_case"]["company_id"] == claims["company_id"]]

    def visible_source_docs(self, claims: dict, masked: bool = True) -> list[dict]:
        src = self.masked if masked else self.raw
        cut = self.cutoff(claims)
        return [d for d in src[claims["work_id"]]["source_documents"] if d["uploaded_at"] <= cut]

    # ── 근거 ID 풀 (save_* 검증용, 명세 2장 근거 ID 체계) ──
    def evidence_pool(self, claims: dict) -> set[str]:
        wid = claims["work_id"]
        raw = self.raw[wid]
        snap = claims["snapshot"]
        rng = snap["conversation_range"]
        pool: set[str] = set()
        pool |= {m["message_id"] for m in raw["messages"] if rng["from_seq"] <= m["seq"] <= rng["to_seq"]}
        wc = raw["work_case"]
        if wc.get("registration") is not None:
            pool.add(f"{wid}:registration")
        if wc.get("source_message"):
            pool.add(f"{wid}:source_message")
            for i, _ in enumerate(wc["source_message"]["content"].split("\n"), 1):
                pool.add(f"{wid}:source_message:L{i:02d}")
        versions = set(self.allowed_versions(claims))
        for v in raw["artifact_versions"]:
            if v["version"] in versions:
                pool |= {s["span_id"] for s in v["spans"]}
        for lw in self.linked_case_works(claims):
            for v in self.raw[lw]["artifact_versions"]:
                pool |= {s["span_id"] for s in v["spans"]}
        pool |= {l["link_id"] for l in raw["evidence_links"] if l["span_id"] in pool}
        pool |= {a["approval_id"] for a in raw.get("approval_requests", []) if a["artifact_version"] in versions}
        for d in self.visible_source_docs(claims, masked=False):
            pool.add(d["attachment_id"])
            for i, _ in enumerate(d["content"].split("\n"), 1):
                pool.add(f"{d['attachment_id']}:L{i:02d}")
        for i, _ in enumerate(raw["reference_access_log"], 1):
            pool.add(f"{wid}:ACC-{i:02d}")
        for ref in snap["reference_refs"] + snap.get("linked_doc_refs", []):
            d = self.docs.get((ref["doc_id"], ref["version"]))
            if d and d.get("indexed"):
                pool.add(f"{d['doc_id']}@{d['version']}")
                pool |= {f"{d['doc_id']}@{d['version']}#{c['chunk_id']}" for c in d.get("chunks", [])}
        for d in self.documents:
            if d.get("collection") == "policies" and d.get("indexed"):
                pool.add(f"{d['doc_id']}@{d['version']}")
                pool |= {f"{d['doc_id']}@{d['version']}#{c['chunk_id']}" for c in d["chunks"]}
        if wc["work_type"] == "quote":
            pool.add(f"{wid}:reference_quote")
        pool.add(f"{wid}:policy_check")
        return pool

    # ── 조회 도구 구현 ──
    def get_work_case(self, claims: dict, work_id: str) -> dict:
        self.own_work(claims, work_id)
        m = self.masked[work_id]
        wc = m["work_case"]
        version = claims["snapshot"]["artifact_ref"]["version"]
        cust = self.customers.get(wc["customer_id"], {})
        sm = wc.get("source_message")
        source_message = None
        if sm:
            source_message = {
                "evidence_id": f"{work_id}:source_message",
                "channel": sm.get("channel"),
                "received_at": sm.get("received_at"),
                "content": sm["content"],
                "content_type": "data",
                "lines": [
                    {"line_id": f"{work_id}:source_message:L{i:02d}", "text": t}
                    for i, t in enumerate(sm["content"].split("\n"), 1)
                ],
            }
        allowed = set(self.allowed_versions(claims))
        out = {
            "work_id": work_id,
            "work_type": wc["work_type"],
            "customer": {"customer_id": wc["customer_id"], "name": cust.get("name")},
            "employee_id": wc["employee_id"],
            "team_id": wc.get("team_id"),
            "status": wc["status"],
            "created_at": wc["created_at"],
            "completed_at": self.completed_at(work_id, version),
            "rfp_status": wc.get("rfp_status"),
            "registration": wc.get("registration"),
            "registration_evidence_id": f"{work_id}:registration" if wc.get("registration") is not None else None,
            "source_message": source_message,
            "source_documents": [
                {
                    "attachment_id": d["attachment_id"],
                    "doc_id": d["doc_id"],
                    "doc_type": d.get("doc_type"),
                    "version": d["version"],
                    "grade": d.get("grade"),
                    "uploaded_at": d["uploaded_at"],
                    "line_count": len(d["content"].split("\n")),
                }
                for d in self.visible_source_docs(claims)
            ],
            "attached_references": claims["snapshot"]["reference_refs"],
            "artifact_refs": [
                {
                    "artifact_id": v["artifact_id"],
                    "version": v["version"],
                    "author": v["author"],
                    "saved_at": v["saved_at"],
                    "is_evaluation_target": v["version"] == version,
                }
                for v in m["artifact_versions"]
                if v["version"] in allowed
            ],
            "linked_work_ids": wc["linked_work_ids"],
        }
        if wc["work_type"] == "proposal":
            out["reused_sources"] = wc.get("reused_sources", [])
        return out

    def get_conversation(self, claims: dict, work_id: str, from_seq: int | None, to_seq: int | None, limit: int | None) -> dict:
        self.own_work(claims, work_id)
        rng = claims["snapshot"]["conversation_range"]
        f = rng["from_seq"] if from_seq is None else from_seq
        t = rng["to_seq"] if to_seq is None else to_seq
        if f < rng["from_seq"] or t > rng["to_seq"] or f > t and rng["to_seq"] > 0:
            raise version_conflict(
                f"요청 구간 {f}~{t}이 스냅샷 대화 범위 {rng['from_seq']}~{rng['to_seq']} 밖입니다"
            )
        limit = 50 if limit is None else limit
        if not 1 <= limit <= 50:
            raise validation("limit은 1~50입니다")
        msgs = [m for m in self.masked[work_id]["messages"] if f <= m["seq"] <= t]
        page = msgs[:limit]
        nxt = msgs[limit]["seq"] if len(msgs) > limit else None
        return {
            "work_id": work_id,
            "conversation_range": rng,
            "messages": [
                {k: m[k] for k in ("message_id", "seq", "role", "content", "created_at")} for m in page
            ],
            "next_seq": nxt,
        }

    def get_source_document(self, claims: dict, attachment_id: str, from_line: int | None, limit: int | None) -> dict:
        owner = self.attachment_owner.get(attachment_id)
        if owner != claims["work_id"]:
            raise forbidden(f"{attachment_id}는 이 실행의 업무 첨부가 아닙니다") if owner else not_found(f"첨부 {attachment_id}")
        doc = next((d for d in self.visible_source_docs(claims) if d["attachment_id"] == attachment_id), None)
        if doc is None:
            raise version_conflict(f"{attachment_id}는 스냅샷 시점 이후에 업로드되었습니다")
        lines = doc["content"].split("\n")
        start = 1 if from_line is None else from_line
        limit = 200 if limit is None else limit
        if start < 1 or limit < 1:
            raise validation("from_line과 limit은 1 이상입니다")
        out_lines = []
        for i in range(start, min(len(lines), start + limit - 1) + 1):
            text = lines[i - 1]
            mm = LOC_RE.match(text)
            out_lines.append({"line_id": f"{attachment_id}:L{i:02d}", "text": text, "location": mm.group(1) if mm else None})
        return {
            "attachment_id": attachment_id,
            "doc_id": doc["doc_id"],
            "doc_type": doc.get("doc_type"),
            "version": doc["version"],
            "grade": doc.get("grade"),
            "uploaded_at": doc["uploaded_at"],
            "content_type": "data",
            "total_lines": len(lines),
            "lines": out_lines,
            "next_line": start + limit if start + limit <= len(lines) else None,
        }

    def _artifact_access(self, claims: dict, artifact_id: str, version: str) -> str:
        owner = self.artifact_owner.get(artifact_id)
        if owner is None:
            raise not_found(f"산출물 {artifact_id}")
        if owner == claims["work_id"]:
            if version not in {v["version"] for v in self.raw[owner]["artifact_versions"]}:
                raise not_found(f"산출물 버전 {artifact_id} {version}")
            if version not in self.allowed_versions(claims):
                raise version_conflict(
                    f"{version}은 스냅샷 버전 {claims['snapshot']['artifact_ref']['version']} 이후에 저장된 버전입니다"
                )
            return owner
        if owner in self.linked_case_works(claims):
            return owner
        raise forbidden(f"{artifact_id}는 이 실행의 업무나 연결 업무의 산출물이 아닙니다")

    def get_artifact_version(self, claims: dict, artifact_id: str, version: str) -> dict:
        owner = self._artifact_access(claims, artifact_id, version)
        v = self.version_rec(owner, version, masked=True)
        return {
            "artifact_id": artifact_id,
            "work_id": owner,
            "version": version,
            "author": v["author"],
            "saved_at": v["saved_at"],
            "is_evaluation_target": owner == claims["work_id"] and version == claims["snapshot"]["artifact_ref"]["version"],
            "content": v["content"],
            "spans": [{"span_id": s["span_id"], "text": s["text"]} for s in v["spans"]],
        }

    def get_artifact_diff(self, claims: dict, artifact_id: str, from_version: str, to_version: str) -> dict:
        owner = self._artifact_access(claims, artifact_id, from_version)
        self._artifact_access(claims, artifact_id, to_version)
        a = self.version_rec(owner, from_version, masked=True)["spans"]
        b = self.version_rec(owner, to_version, masked=True)["spans"]
        return {
            "artifact_id": artifact_id,
            "from_version": from_version,
            "to_version": to_version,
            "changes": diff_spans(a, b),
        }

    def get_linked_works(self, claims: dict, work_id: str) -> dict:
        self.own_work(claims, work_id)
        works = []
        for lw in self.raw[work_id]["work_case"]["linked_work_ids"]:
            if lw in self.raw:
                r = self.raw[lw]
                if r["work_case"]["company_id"] != claims["company_id"]:
                    continue
                emp = [v for v in r["artifact_versions"] if v["author"] == "employee"]
                last = emp[-1] if emp else r["artifact_versions"][-1]
                works.append({
                    "work_id": lw,
                    "work_type": r["work_case"]["work_type"],
                    "customer_id": r["work_case"]["customer_id"],
                    "status": r["work_case"]["status"],
                    "completed_at": r["work_case"].get("completed_at"),
                    "final_artifact": {"artifact_id": last["artifact_id"], "version": last["version"]},
                    "evaluated": True,
                })
            elif lw in self.historical:
                h = self.historical[lw]
                if h["company_id"] != claims["company_id"]:
                    continue
                works.append({
                    "work_id": lw,
                    "work_type": h["work_type"],
                    "customer_id": h["customer_id"],
                    "status": h["status"],
                    "completed_at": h.get("completed_at"),
                    "final_artifact": h["final_artifact"],
                    "evaluated": h.get("evaluated", False),
                })
        return {"work_id": work_id, "works": works}

    def get_reference_access_log(self, claims: dict, work_id: str) -> dict:
        self.own_work(claims, work_id)
        raw = self.raw[work_id]
        emp = raw["work_case"]["employee_id"]
        return {
            "work_id": work_id,
            "note": "열람 기록만으로는 결과 검증으로 인정하지 않는다",
            "accesses": [
                {"access_id": f"{work_id}:ACC-{i:02d}", "doc_id": a["doc_id"], "version": a["version"], "accessed_at": a["accessed_at"]}
                for i, a in enumerate(raw["reference_access_log"], 1)
                if a["user_id"] == emp
            ],
        }

    def get_evidence_links(self, claims: dict, work_id: str) -> dict:
        self.own_work(claims, work_id)
        pool_versions = set(self.allowed_versions(claims))
        links = []
        for l in self.raw[work_id]["evidence_links"]:
            ver = l["span_id"].split(":")[-2]
            if ver not in pool_versions:
                continue
            links.append({k: l.get(k) for k in ("link_id", "span_id", "doc_id", "version", "location", "created_at")})
        return {"work_id": work_id, "links": links}

    def get_approval_requests(self, claims: dict, work_id: str) -> dict:
        self.own_work(claims, work_id)
        versions = set(self.allowed_versions(claims))
        reqs = [
            {
                "approval_id": a["approval_id"],
                "artifact_version": a["artifact_version"],
                "submitted_at": a["submitted_at"],
                "items": [{k: i.get(k) for k in ("type", "value", "approver", "reason")} for i in a["items"]],
            }
            for a in self.raw[work_id].get("approval_requests", [])
            if a["artifact_version"] in versions
        ]
        return {"work_id": work_id, "requests": reqs}

    def human_action(self, claims: dict, evaluation: dict, action: str) -> dict | None:
        wid = evaluation["work_id"]
        as_of = claims.get("as_of")
        hits = [
            h
            for h in self.masked[wid]["human_actions"]
            if h["action"] == action
            and h.get("artifact_version", evaluation["artifact_version"]) == evaluation["artifact_version"]
            and (as_of is None or h["at"] <= as_of)
        ]
        return hits[-1] if hits else None


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def diff_spans(a: list[dict], b: list[dict]) -> list[dict]:
    """같은 구간 키(REQ-06 등)는 키로, Lnn 줄 구간은 버전 간 줄 밀림을 텍스트 유사도로 보정해 맞춘다."""
    changes: list[dict] = []

    def ch(t, sa, sb):
        changes.append({
            "type": t,
            "span_key": span_key((sb or sa)["span_id"]),
            "before_span_id": sa["span_id"] if sa else None,
            "after_span_id": sb["span_id"] if sb else None,
            "before": sa["text"] if sa else None,
            "after": sb["text"] if sb else None,
        })

    ka = [s for s in a if not LINE_KEY_RE.match(span_key(s["span_id"]))]
    kb = [s for s in b if not LINE_KEY_RE.match(span_key(s["span_id"]))]
    ma = {span_key(s["span_id"]): s for s in ka}
    mb = {span_key(s["span_id"]): s for s in kb}
    for k in list(ma) + [k for k in mb if k not in ma]:
        sa, sb = ma.get(k), mb.get(k)
        if sa and sb:
            if sa["text"] != sb["text"]:
                ch("modified", sa, sb)
        elif sa:
            ch("removed", sa, None)
        else:
            ch("added", None, sb)

    la = [s for s in a if LINE_KEY_RE.match(span_key(s["span_id"]))]
    lb = [s for s in b if LINE_KEY_RE.match(span_key(s["span_id"]))]
    sm = SequenceMatcher(None, [s["text"] for s in la], [s["text"] for s in lb], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        xa, xb = la[i1:i2], lb[j1:j2]
        if tag == "replace":
            # 남는 줄끼리 유사도가 높은 순서대로 짝을 짓는다
            used_b: set[int] = set()
            for sa in xa:
                best, bi = 0.0, None
                for j, sb in enumerate(xb):
                    if j in used_b:
                        continue
                    r = _sim(sa["text"], sb["text"])
                    if r > best:
                        best, bi = r, j
                if bi is not None and best >= 0.4:
                    used_b.add(bi)
                    ch("modified", sa, xb[bi])
                else:
                    ch("removed", sa, None)
            for j, sb in enumerate(xb):
                if j not in used_b:
                    ch("added", None, sb)
        elif tag == "delete":
            for sa in xa:
                ch("removed", sa, None)
        elif tag == "insert":
            for sb in xb:
                ch("added", None, sb)
    return changes
