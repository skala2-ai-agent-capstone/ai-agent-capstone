"""business-mcp의 PostgreSQL 접근 계층.

- Db: 연결 하나를 잠금으로 보호해 쓴다(조회는 autocommit, 쓰기는 트랜잭션). 끊기면 한 번 다시 연결한다.
- Repo: 테이블을 읽어 기존 시드·케이스 JSON과 같은 모양의 dict를 만든다. 도구 로직(store·policy·quote·writes)은
  이 모양만 알면 되므로 저장소가 바뀌어도 도구 계약은 그대로다.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

KST = timezone(timedelta(hours=9))


def iso(v):
    """timestamptz → '2026-10-16T09:45:00+09:00' (케이스 원본과 같은 표기), date → '2026-07-01'."""
    if isinstance(v, datetime):
        return v.astimezone(KST).isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def num(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class Db:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.lock = threading.RLock()
        self.conn = self._connect()

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def _run(self, fn):
        with self.lock:
            try:
                return fn(self.conn)
            except psycopg.OperationalError:
                self.conn = self._connect()
                return fn(self.conn)

    def q(self, sql: str, params=()) -> list[dict]:
        return self._run(lambda c: c.execute(sql, params).fetchall())

    def one(self, sql: str, params=()) -> dict | None:
        return self._run(lambda c: c.execute(sql, params).fetchone())

    @contextmanager
    def tx(self):
        """쓰기 트랜잭션. 블록 안에서는 self.conn을 직접 쓴다."""
        with self.lock:
            if self.conn.closed:
                self.conn = self._connect()
            with self.conn.transaction():
                yield self.conn


class Repo:
    def __init__(self, db: Db):
        self.db = db

    # ── 기준 데이터 (시드 JSON 모양) ──
    def organization(self) -> dict:
        return {
            "companies": self.db.q("SELECT company_id, name, min_aggregation_size FROM companies ORDER BY company_id"),
            "teams": self.db.q("SELECT team_id, company_id, name FROM teams ORDER BY team_id"),
            "users": self.db.q("SELECT user_id, name, company_id, team_id, role FROM users ORDER BY user_id"),
        }

    def customers(self) -> list[dict]:
        return self.db.q("SELECT customer_id, company_id, name, approved_reference FROM customers ORDER BY customer_id")

    def pricing(self) -> dict:
        tables = []
        for t in self.db.q("SELECT * FROM price_tables ORDER BY effective_from NULLS LAST, version"):
            prices = {r["code"]: num(r["unit_price"]) for r in self.db.q(
                "SELECT code, unit_price FROM price_table_items WHERE version=%s ORDER BY code", (t["version"],))}
            tables.append({"version": t["version"], "status": t["status"], "effective_from": iso(t["effective_from"]),
                           "effective_to": iso(t["effective_to"]), "prices": prices})
        meta = {r["key"]: r["value"] for r in self.db.q("SELECT key, value FROM pricing_policy")}
        return {
            "products": self.db.q("SELECT code, name, unit FROM products ORDER BY code"),
            "price_tables": tables,
            "discount_rules": {
                "components": [{"type": r["type"], "condition": r["condition"], "pct": num(r["pct"])}
                               for r in self.db.q("SELECT * FROM discount_components ORDER BY seq")],
                "approval_tiers": [{"max_pct": num(r["max_pct"]), "approver": r["approver"], "approval_required": r["approval_required"]}
                                   for r in self.db.q("SELECT * FROM approval_tiers ORDER BY max_pct")],
                "exceptions": self.db.q("SELECT type, approver FROM exception_rules ORDER BY type"),
                **meta,
            },
        }

    def policy_rules(self) -> dict:
        rules = []
        for r in self.db.q("SELECT * FROM policy_rules ORDER BY seq"):
            rules.append({k: v for k, v in r.items() if k != "seq" and v is not None})
        meta = self.db.one("SELECT value FROM policy_meta WHERE key='scope_note'")
        return {"scope_note": meta["value"] if meta else None, "rules": rules}

    def documents(self) -> list[dict]:
        out = []
        for d in self.db.q("SELECT * FROM documents ORDER BY doc_id, version"):
            doc = {k: (iso(v) if k == "effective_from" else v) for k, v in d.items()}
            doc["chunks"] = self.db.q(
                "SELECT chunk_id, clause_id, label, text FROM document_chunks WHERE doc_id=%s AND version=%s ORDER BY seq",
                (d["doc_id"], d["version"]))
            if d["collection"] == "rubrics":
                doc["items"] = self.db.q(
                    "SELECT item_id, item, criterion, score_2, score_1, score_0, unobserved, needs_review, evidence_sources, related_clauses "
                    "FROM rubric_items WHERE doc_id=%s AND version=%s ORDER BY seq", (d["doc_id"], d["version"]))
            if d["collection"] == "coaching_examples":
                doc["text"] = d["content"]
            out.append(doc)
        return out

    def historical(self) -> list[dict]:
        out = []
        for h in self.db.q("SELECT * FROM historical_works ORDER BY work_id"):
            out.append({"work_id": h["work_id"], "company_id": h["company_id"], "team_id": h["team_id"], "employee_id": h["employee_id"],
                        "work_type": h["work_type"], "customer_id": h["customer_id"], "status": h["status"],
                        "completed_at": iso(h["completed_at"]),
                        "final_artifact": {"doc_id": h["final_doc_id"], "version": h["final_doc_version"]} if h["final_doc_id"] else {},
                        "evaluated": h["evaluated"], "note": h["note"]})
        return out

    def fault_plans(self, prefix: str) -> dict[str, list[dict]]:
        plans: dict[str, list] = {}
        for r in self.db.q("SELECT work_id, target, error, times, on_call FROM fault_injections WHERE target LIKE %s ORDER BY work_id, seq",
                           (prefix + ".%",)):
            plans.setdefault(r["work_id"], []).append({k: r[k] for k in ("target", "error", "times", "on_call")})
        return plans

    # ── 소유 관계 조회 ──
    def artifact_owner(self, artifact_id: str) -> str | None:
        r = self.db.one("SELECT work_id FROM artifact_versions WHERE artifact_id=%s LIMIT 1", (artifact_id,))
        return r["work_id"] if r else None

    def attachment_owner(self, attachment_id: str) -> str | None:
        r = self.db.one("SELECT work_id FROM source_documents WHERE attachment_id=%s", (attachment_id,))
        return r["work_id"] if r else None

    def case_id(self, work_id: str) -> str | None:
        r = self.db.one("SELECT case_id FROM work_cases WHERE work_id=%s", (work_id,))
        return r["case_id"] if r else None

    # ── 업무 한 건 (케이스 raw 모양) ──
    def work(self, work_id: str, masked: bool) -> dict | None:
        q, one = self.db.q, self.db.one
        w = one("SELECT * FROM work_cases WHERE work_id=%s", (work_id,))
        if w is None:
            return None
        sfx = "_masked" if masked else ""
        wc = {
            "work_id": w["work_id"], "company_id": w["company_id"], "team_id": w["team_id"], "employee_id": w["employee_id"],
            "work_type": w["work_type"], "customer_id": w["customer_id"], "status": w["status"], "created_at": iso(w["created_at"]),
            "completed_at": iso(w["completed_at"]), "rfp_status": w["rfp_status"],
            "registration": w["registration" + sfx],
            "linked_work_ids": [r["linked_work_id"] for r in q("SELECT linked_work_id FROM work_links WHERE work_id=%s ORDER BY seq", (work_id,))],
        }
        sm = one(f"SELECT channel, received_at, content{sfx} AS content FROM source_messages WHERE work_id=%s", (work_id,))
        if sm:
            wc["source_message"] = {"channel": sm["channel"], "received_at": iso(sm["received_at"]), "content": sm["content"]}
        reused = q("SELECT doc_id, version, recorded_at FROM reused_sources WHERE work_id=%s ORDER BY recorded_at", (work_id,))
        if reused:
            wc["reused_sources"] = [{**r, "recorded_at": iso(r["recorded_at"])} for r in reused]

        versions = []
        for v in q(f"SELECT artifact_id, version, author, saved_at, content{sfx} AS content FROM artifact_versions "
                   "WHERE work_id=%s ORDER BY saved_at, version", (work_id,)):
            spans = q(f"SELECT span_id, text{sfx} AS text FROM artifact_spans WHERE artifact_id=%s AND version=%s ORDER BY seq",
                      (v["artifact_id"], v["version"]))
            versions.append({**v, "saved_at": iso(v["saved_at"]), "spans": spans})

        approvals = []
        for a in q("SELECT approval_id, artifact_version, submitted_at FROM approval_requests WHERE work_id=%s ORDER BY submitted_at", (work_id,)):
            items = q("SELECT type, value, approver, reason FROM approval_request_items WHERE approval_id=%s ORDER BY seq", (a["approval_id"],))
            approvals.append({**a, "submitted_at": iso(a["submitted_at"]), "items": items})

        human = []
        for h in q(f"SELECT action_id, user_id, action, at, artifact_version, payload{sfx} AS payload FROM human_actions "
                   "WHERE work_id=%s ORDER BY at, action_id", (work_id,)):
            rec = {"action_id": h["action_id"], "user_id": h["user_id"], "action": h["action"], "at": iso(h["at"]), **(h["payload"] or {})}
            if h["artifact_version"]:
                rec["artifact_version"] = h["artifact_version"]
            human.append(rec)

        return {
            "work_case": wc,
            "source_documents": [
                {**d, "uploaded_at": iso(d["uploaded_at"])}
                for d in q(f"SELECT attachment_id, doc_id, doc_type, version, grade, uploaded_by, uploaded_at, content{sfx} AS content "
                           "FROM source_documents WHERE work_id=%s ORDER BY uploaded_at, attachment_id", (work_id,))
            ],
            "attached_references": q("SELECT doc_id, version FROM attached_references WHERE work_id=%s ORDER BY seq", (work_id,)),
            "messages": [
                {**m, "created_at": iso(m["created_at"])}
                for m in q(f"SELECT message_id, seq, role, content{sfx} AS content, created_at FROM messages WHERE work_id=%s ORDER BY seq", (work_id,))
            ],
            "artifact_versions": versions,
            "reference_access_log": [
                {**a, "accessed_at": iso(a["accessed_at"])}
                for a in q("SELECT user_id, doc_id, version, accessed_at FROM reference_access_logs WHERE work_id=%s ORDER BY seq", (work_id,))
            ],
            "evidence_links": [
                {**l, "created_at": iso(l["created_at"])}
                for l in q("SELECT link_id, span_id, doc_id, version, location, created_by, created_at FROM evidence_links "
                           "WHERE work_id=%s ORDER BY created_at, link_id", (work_id,))
            ],
            "events": [
                {k: v for k, v in {**e, "occurred_at": iso(e["occurred_at"])}.items() if v is not None}
                for e in q("SELECT event_id, event_type, work_id, artifact_version, rubric_version, idempotency_key, occurred_at "
                           "FROM work_events WHERE work_id=%s ORDER BY occurred_at, event_id", (work_id,))
            ],
            "human_actions": human,
            "quote_data": {
                r["artifact_version"]: {"quote_no": r["quote_no"], "price_table": r["price_table"], "lines": r["lines"],
                                        "discount_components": r["discount_components"], "free_items": r["free_items"],
                                        "exceptions": r["exceptions"]}
                for r in q("SELECT * FROM quote_data WHERE work_id=%s", (work_id,))
            },
            "approval_requests": approvals,
        }
