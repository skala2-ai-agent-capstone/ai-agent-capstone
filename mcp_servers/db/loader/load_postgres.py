"""E2E raw 데이터(seed/ + 케이스 40건)를 PostgreSQL에 적재한다.

    python -m db.loader.load_postgres --data data            # 폴더
    python -m db.loader.load_postgres --data e2e_raw_40.zip  # 받은 zip 그대로
    옵션: --reset (백엔드 테이블까지 전체 비우고 다시 적재), --schema db/postgres/001_schema.sql (스키마 먼저 적용)

- 텍스트는 원문과 마스킹본(policy_rules의 MASK-* 규칙)을 같이 넣는다.
- 대화는 messages 테이블이 아니라 llm_exchanges에 넣는다. messages는 그것을 펼친 뷰다.
  data/llm_exchanges.jsonl(모의 데이터 생성기 출력)이 있으면 그대로 쓰고, 없으면 케이스 대화를 chat_log 규칙으로 바꾼다.
- 시드 사용자는 사번(user_id의 숫자)·역할(member/team_lead)로 바꿔 넣는다. 비밀번호는 SEED_USER_PASSWORD 값.
- work.completed 이벤트를 중복 키로 묶어 평가 건(evaluations)을 만든다 (백엔드가 하는 일의 테스트용 재현).
- 다시 실행해도 같은 결과가 나오도록 업무 단위로 지우고 넣는다. 평가 결과 테이블은 --reset일 때만 비운다.
- --reset은 같은 DB의 백엔드 테이블(outbox_events·evaluation_failures·aggregation_approvals)도 비운다.
  백엔드를 멈추고 RabbitMQ 큐를 비운 뒤 실행한다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import defaultdict

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.runtime import Masker, eval_id_for  # noqa: E402
from db.loader.chat_log import workspace_exchanges  # noqa: E402

RESULT_TABLES = ["audit_log", "idempotency_keys", "progress_log", "review_requests", "evaluation_coaching", "evaluation_drafts",
                 "live_evaluations", "evaluations"]
WORK_TABLES = ["access_attempts", "fault_injections", "approval_request_items", "approval_requests", "quote_data", "human_actions",
               "work_events", "evidence_links", "reference_access_logs", "artifact_spans", "artifact_versions", "llm_exchanges",
               "attached_references", "source_documents", "reused_sources", "work_links", "source_messages", "work_cases"]
# 시드의 예전 역할 → users.role. 검토자는 팀장이 겸임한다
ROLE_MAP = {"team_lead": "team_lead", "reviewer": "team_lead", "member": "member"}
EXCHANGE_COLUMNS = ["created_at", "member", "endpoint", "stage", "work_id", "user_id", "model", "stream", "status_code", "error",
                    "latency_ms", "input_tokens", "output_tokens", "request", "response", "response_text"]
SEED_TABLES = ["historical_works", "rubric_items", "document_chunks", "documents", "policy_meta", "policy_rules", "pricing_policy",
               "exception_rules", "approval_tiers", "discount_components", "price_table_items", "price_tables", "products",
               "customers", "users", "teams", "companies"]
# 백엔드(aworko-server Flyway)가 같은 DB에 만드는 테이블. --reset일 때만 같이 비운다(flyway_backend_history는 남긴다)
BACKEND_TABLES = ["outbox_events", "evaluation_failures", "aggregation_approvals"]


def resolve_data(path: str) -> str:
    """폴더 또는 zip을 받아 seed/와 케이스가 있는 폴더 경로를 돌려준다."""
    if path.endswith(".zip"):
        tmp = tempfile.mkdtemp(prefix="e2e_raw_")
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        path = tmp
    for root, dirs, _ in os.walk(path):
        if "seed" in dirs and (os.path.exists(os.path.join(root, "all_cases.jsonl")) or "cases" in dirs):
            return root
    raise SystemExit(f"{path}에서 seed/와 all_cases.jsonl(또는 cases/)을 찾지 못했습니다")


def read_cases(root: str) -> list[dict]:
    p = os.path.join(root, "all_cases.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    out = []
    for fp in sorted(glob.glob(os.path.join(root, "cases", "*", "*.json"))):
        with open(fp, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def j(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_exchanges(root: str, cases: list[dict]) -> dict[str | None, list[dict]]:
    """업무별 llm_exchanges 행(시간순). 키 None은 업무가 없는 호출(개발 테스트 등)."""
    out: dict[str | None, list[dict]] = defaultdict(list)
    p = os.path.join(root, "llm_exchanges.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in sorted(rows, key=lambda r: (r["created_at"], r.get("id", 0))):
            out[r.get("work_id")].append(r)
        return out
    for case in cases:
        out[case["raw"]["work_case"]["work_id"]] = workspace_exchanges(case)
    return out


def insert_exchanges(ex, rows: list[dict]) -> None:
    for r in rows:
        values = [Jsonb(r.get(c)) if c in ("request", "response") and r.get(c) is not None else r.get(c) for c in EXCHANGE_COLUMNS]
        ex(f"INSERT INTO llm_exchanges ({', '.join(EXCHANGE_COLUMNS)}) VALUES ({', '.join(['%s'] * len(EXCHANGE_COLUMNS))})", values)


def employee_no(user: dict) -> int:
    if user.get("employee_no") is not None:
        return int(user["employee_no"])
    digits = re.sub(r"\D", "", user["user_id"])
    if not digits:
        raise SystemExit(f"사번을 만들 수 없는 사용자 ID입니다: {user['user_id']} (organization.json에 employee_no를 넣으세요)")
    return int(digits)


def load(conn: psycopg.Connection, root: str, reset: bool) -> dict:
    seed = os.path.join(root, "seed")
    org, customers, pricing = j(f"{seed}/organization.json"), j(f"{seed}/customers.json"), j(f"{seed}/pricing.json")
    policy, documents, historical = j(f"{seed}/policy_rules.json"), j(f"{seed}/documents.json"), j(f"{seed}/historical_works.json")
    cases = read_cases(root)
    exchanges = read_exchanges(root, cases)
    seed_password = os.environ.get("SEED_USER_PASSWORD", "")
    if not seed_password:
        raise SystemExit("SEED_USER_PASSWORD 환경 변수가 필요합니다(시드 사용자 로그인 비밀번호, .env에 설정)")
    masker = Masker(policy)
    tenant = org["companies"][0]["company_id"]
    warnings: list[str] = []
    cur = conn.cursor()
    ex = cur.execute

    backend_cleared: list[str] = []
    if reset:
        # CASCADE만 믿으면 FK가 있는 evaluation_failures·aggregation_approvals만 비고 outbox_events는 남아,
        # 다시 만든 같은 ID의 평가 건으로 이전 메시지가 발행된다. 백엔드를 아직 안 띄운 DB에는 테이블이 없다
        backend_cleared = [t for t in BACKEND_TABLES if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]]
        ex("TRUNCATE " + ", ".join(RESULT_TABLES + WORK_TABLES + SEED_TABLES + backend_cleared) + " CASCADE")

    # ── 기준 데이터: upsert ──
    for c in org["companies"]:
        ex("INSERT INTO companies VALUES (%s,%s,%s) ON CONFLICT (company_id) DO UPDATE SET name=EXCLUDED.name, min_aggregation_size=EXCLUDED.min_aggregation_size",
           (c["company_id"], c["name"], c.get("min_aggregation_size", 5)))
    for t in org["teams"]:
        ex("INSERT INTO teams VALUES (%s,%s,%s) ON CONFLICT (team_id) DO UPDATE SET company_id=EXCLUDED.company_id, name=EXCLUDED.name",
           (t["team_id"], t["company_id"], t["name"]))
    for u in org["users"]:
        # 이미 있는 사용자의 비밀번호는 덮어쓰지 않는다(앱에서 바꾼 비밀번호 유지)
        ex("INSERT INTO users (user_id, employee_no, password, company_id, team_id, name, role) VALUES (%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (user_id) DO UPDATE SET employee_no=EXCLUDED.employee_no, company_id=EXCLUDED.company_id, "
           "team_id=EXCLUDED.team_id, name=EXCLUDED.name, role=EXCLUDED.role",
           (u["user_id"], employee_no(u), seed_password, u["company_id"], u.get("team_id"), u["name"], ROLE_MAP.get(u["role"], "member")))
    for c in customers:
        ex("INSERT INTO customers VALUES (%s,%s,%s,%s) ON CONFLICT (customer_id) DO UPDATE SET name=EXCLUDED.name, "
           "approved_reference=EXCLUDED.approved_reference", (c["customer_id"], c.get("company_id", tenant), c["name"], c["approved_reference"]))

    for t in ["price_table_items", "price_tables", "discount_components", "approval_tiers", "exception_rules", "pricing_policy",
              "policy_rules", "policy_meta"]:
        ex(f"DELETE FROM {t}")
    for p in pricing["products"]:
        ex("INSERT INTO products VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, unit=EXCLUDED.unit",
           (p["code"], p["name"], p["unit"]))
    for t in pricing["price_tables"]:
        ex("INSERT INTO price_tables VALUES (%s,%s,%s,%s)", (t["version"], t["status"], t.get("effective_from"), t.get("effective_to")))
        for code, price in t["prices"].items():
            ex("INSERT INTO price_table_items VALUES (%s,%s,%s)", (t["version"], code, price))
    rules = pricing["discount_rules"]
    for i, c in enumerate(rules["components"], 1):
        ex("INSERT INTO discount_components VALUES (%s,%s,%s,%s)", (i, c["type"], c["condition"], c["pct"]))
    for t in rules["approval_tiers"]:
        ex("INSERT INTO approval_tiers VALUES (%s,%s,%s)", (t["max_pct"], t["approver"], t["approval_required"]))
    for e in rules["exceptions"]:
        ex("INSERT INTO exception_rules VALUES (%s,%s)", (e["type"], e["approver"]))
    for k in ("rounding", "source"):
        if rules.get(k):
            ex("INSERT INTO pricing_policy VALUES (%s,%s)", (k, rules[k]))

    for i, r in enumerate(policy["rules"], 1):
        ex("INSERT INTO policy_rules VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (r["rule_id"], i, r["kind"], r.get("pattern"), r.get("replace_with"), r.get("targets"), r.get("clause"),
            r.get("description"), r.get("detect")))
    if policy.get("scope_note"):
        ex("INSERT INTO policy_meta VALUES ('scope_note', %s)", (policy["scope_note"],))

    ex("DELETE FROM documents")  # chunks·rubric_items는 CASCADE
    for d in documents:
        content = d.get("content")
        if d.get("collection") == "coaching_examples":
            content = d.get("text")
        ex("INSERT INTO documents VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (d["doc_id"], d["version"], d.get("company_id", tenant), d["title"], d["grade"], d.get("collection"), d["indexed"],
            d["status"], d.get("effective_from"), content, d.get("work_type"), d.get("item"), d.get("score_level"),
            d.get("source_file"), d.get("note")))
        for i, c in enumerate(d.get("chunks") or [], 1):
            ex("INSERT INTO document_chunks VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (d["doc_id"], d["version"], c["chunk_id"], i, c.get("clause_id"), c.get("label"), c["text"]))
        for i, it in enumerate(d.get("items") or [], 1):
            ex("INSERT INTO rubric_items VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (d["doc_id"], d["version"], it["item_id"], i, it["item"], it["criterion"], it["score_2"], it.get("score_1"),
                it["score_0"], it.get("unobserved"), it.get("needs_review"), it["evidence_sources"], it["related_clauses"]))

    for h in historical:
        fa = h.get("final_artifact") or {}
        ex("INSERT INTO historical_works VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (work_id) DO UPDATE SET "
           "status=EXCLUDED.status, completed_at=EXCLUDED.completed_at, final_doc_id=EXCLUDED.final_doc_id, "
           "final_doc_version=EXCLUDED.final_doc_version, evaluated=EXCLUDED.evaluated, note=EXCLUDED.note",
           (h["work_id"], h["company_id"], h.get("team_id"), h.get("employee_id"), h["work_type"], h.get("customer_id"), h["status"],
            h.get("completed_at"), fa.get("doc_id"), fa.get("version"), h.get("evaluated", False), h.get("note")))

    # ── 업무 데이터: 업무 단위로 지우고 다시 넣는다 ──
    missing_exc = []
    for case in cases:
        raw = case["raw"]
        wc = raw["work_case"]
        wid = wc["work_id"]
        # 이 업무를 참조하는 평가 결과가 있으면 유지해야 하므로 업무 행은 upsert, 하위 데이터만 교체
        for t in WORK_TABLES[:-1]:
            if t in ("artifact_spans", "approval_request_items"):
                continue
            ex(f"DELETE FROM {t} WHERE work_id=%s", (wid,))
        reg = wc.get("registration")
        summary = case.get("summary")
        title = wc.get("title") or (summary.split(" (")[0] if summary else None)
        ex("INSERT INTO work_cases (work_id, case_id, company_id, team_id, employee_id, work_type, customer_id, title, status, "
           "created_at, completed_at, rfp_status, registration, registration_masked, scenario_summary) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (work_id) DO UPDATE SET "
           "case_id=EXCLUDED.case_id, company_id=EXCLUDED.company_id, team_id=EXCLUDED.team_id, employee_id=EXCLUDED.employee_id, "
           "work_type=EXCLUDED.work_type, customer_id=EXCLUDED.customer_id, title=EXCLUDED.title, status=EXCLUDED.status, "
           "created_at=EXCLUDED.created_at, completed_at=EXCLUDED.completed_at, rfp_status=EXCLUDED.rfp_status, "
           "registration=EXCLUDED.registration, registration_masked=EXCLUDED.registration_masked, "
           "scenario_summary=EXCLUDED.scenario_summary",
           (wid, case.get("case_id"), wc["company_id"], wc.get("team_id"), wc["employee_id"], wc.get("work_type"), wc["customer_id"],
            title, wc["status"], wc["created_at"], wc.get("completed_at"), wc.get("rfp_status") or case.get("rfp_status"),
            Jsonb(reg) if reg is not None else None, Jsonb(masker.mask_obj(reg)) if reg is not None else None, summary))
        sm = wc.get("source_message")
        if sm:
            ex("INSERT INTO source_messages VALUES (%s,%s,%s,%s,%s)",
               (wid, sm.get("channel"), sm.get("received_at"), sm["content"], masker.mask(sm["content"])))
        for i, lw in enumerate(wc.get("linked_work_ids", []), 1):
            ex("INSERT INTO work_links VALUES (%s,%s,%s)", (wid, lw, i))
        for r in wc.get("reused_sources", []) or []:
            ex("INSERT INTO reused_sources VALUES (%s,%s,%s,%s)", (wid, r["doc_id"], r["version"], r.get("recorded_at")))
        for d in raw["source_documents"]:
            ex("INSERT INTO source_documents VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (d["attachment_id"], wid, d["doc_id"], d.get("doc_type", "rfp"), d["version"], d.get("grade"), d.get("uploaded_by"),
                d["uploaded_at"], d["content"], masker.mask(d["content"])))
        for i, r in enumerate(raw["attached_references"], 1):
            ex("INSERT INTO attached_references VALUES (%s,%s,%s,%s)", (wid, r["doc_id"], r["version"], i))
        insert_exchanges(ex, exchanges.get(wid, []))
        for v in raw["artifact_versions"]:
            ex("INSERT INTO artifact_versions VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (v["artifact_id"], v["version"], wid, v["author"], v["saved_at"], v["content"], masker.mask(v["content"])))
            for i, sp in enumerate(v["spans"], 1):
                ex("INSERT INTO artifact_spans VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (sp["span_id"], v["artifact_id"], v["version"], i, sp["span_id"].rsplit(":", 1)[1], sp["text"], masker.mask(sp["text"])))
        for i, a in enumerate(raw["reference_access_log"], 1):
            ex("INSERT INTO reference_access_logs VALUES (%s,%s,%s,%s,%s,%s)", (wid, i, a["user_id"], a["doc_id"], a["version"], a["accessed_at"]))
        for l in raw["evidence_links"]:
            ex("INSERT INTO evidence_links VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (l["link_id"], wid, l["span_id"], l["doc_id"], l["version"], l.get("location"), l.get("created_by"), l["created_at"]))
        for e in raw["events"]:
            extra = {k: v for k, v in e.items() if k not in ("event_id", "event_type", "work_id", "artifact_version", "rubric_version",
                                                         "idempotency_key", "occurred_at")}
            ex("INSERT INTO work_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (e["event_id"], wid, e["event_type"], e.get("artifact_version"), e.get("rubric_version"), e.get("idempotency_key"),
                e["occurred_at"], Jsonb(extra)))
        for h in raw["human_actions"]:
            payload = {k: v for k, v in h.items() if k not in ("action_id", "user_id", "action", "at", "artifact_version")}
            ex("INSERT INTO human_actions VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (h["action_id"], wid, h["user_id"], h["action"], h["at"], h.get("artifact_version"), Jsonb(payload),
                Jsonb(masker.mask_obj(payload))))
        for ver, q in (raw.get("quote_data") or {}).items():
            if "exceptions" not in q:
                missing_exc.append(f"{wid}:{ver}")
            ex("INSERT INTO quote_data VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (wid, ver, q.get("quote_no"), q.get("price_table"), Jsonb(q.get("lines", [])), Jsonb(q.get("discount_components", [])),
                Jsonb(q.get("free_items", [])), Jsonb(q.get("exceptions", []))))
        for a in raw.get("approval_requests", []) or []:
            ex("INSERT INTO approval_requests VALUES (%s,%s,%s,%s)", (a["approval_id"], wid, a["artifact_version"], a["submitted_at"]))
            for i, it in enumerate(a["items"], 1):
                ex("INSERT INTO approval_request_items VALUES (%s,%s,%s,%s,%s,%s)",
                   (a["approval_id"], i, it["type"], it.get("value"), it.get("approver"), it.get("reason")))
        for i, f in enumerate(raw.get("fault_injection", []) or [], 1):
            ex("INSERT INTO fault_injections VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (wid, i, f["target"], f.get("node"), f.get("error", "TEMPORARY_FAILURE"), f.get("times", 1), f.get("on_call", 1), f.get("note")))
        for a in raw.get("access_attempts", []) or []:
            ex("INSERT INTO access_attempts VALUES (%s,%s,%s,%s,%s)", (a["attempt_id"], wid, a["user_id"], a["action"], a["target"]))
    # 업무 없는 호출(개발 테스트 등)은 같은 호출 주체 단위로 교체한다
    loose = exchanges.get(None, [])
    for member in sorted({r["member"] for r in loose}):
        ex("DELETE FROM llm_exchanges WHERE work_id IS NULL AND member=%s", (member,))
    insert_exchanges(ex, loose)

    # messages 뷰가 원천 대화를 그대로 돌려주는지 확인한다
    for case in cases:
        wid = case["raw"]["work_case"]["work_id"]
        want = [(m["message_id"], m["seq"], m["role"], m["content"]) for m in sorted(case["raw"]["messages"], key=lambda m: m["seq"])]
        got = [tuple(r) for r in conn.execute(
            "SELECT message_id, seq, role, content FROM messages WHERE work_id=%s ORDER BY seq", (wid,)).fetchall()]
        if got != want:
            warnings.append(f"{wid}: messages 뷰가 원천 대화와 다릅니다(원천 {len(want)}개, 뷰 {len(got)}개)")

    if missing_exc:
        warnings.append(
            f"quote_data.exceptions가 없는 견적 {len(missing_exc)}건은 예외 승인 대상(SLA 상향·결제 조건 변경 등)을 계산하지 못합니다. "
            "exceptions가 들어간 최신 데이터로 적재하세요"
        )

    # ── 평가 건 (work.completed 중복 키 제거) ──
    created = 0
    for row in cur.execute(
        "SELECT DISTINCT ON (idempotency_key) e.work_id, e.artifact_version, e.rubric_version, e.idempotency_key, w.company_id, a.artifact_id "
        "FROM work_events e JOIN work_cases w USING (work_id) "
        "JOIN artifact_versions a ON a.work_id=e.work_id AND a.version=e.artifact_version "
        "WHERE e.event_type='work.completed' ORDER BY idempotency_key, occurred_at"
    ).fetchall():
        wid, ver, rver, key, company, art = row
        r = conn.execute(
            "INSERT INTO evaluations (evaluation_id, work_id, company_id, artifact_id, artifact_version, rubric_version, source_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (eval_id_for(wid, ver), wid, company, art, ver, rver, key),
        )
        created += r.rowcount

    counts = {}
    for t in ["users", "work_cases", "llm_exchanges", "messages", "artifact_versions", "artifact_spans", "source_documents", "documents",
              "document_chunks", "rubric_items", "evaluations"]:
        counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    return {"counts": counts, "evaluations_created": created, "backend_cleared": backend_cleared, "warnings": warnings}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--schema", help="적재 전에 실행할 스키마 SQL 파일")
    ap.add_argument("--reset", action="store_true", help="평가 결과·백엔드 테이블까지 전부 비우고 다시 적재")
    a = ap.parse_args()
    if not a.dsn:
        raise SystemExit("DATABASE_URL 환경 변수 또는 --dsn이 필요합니다")
    root = resolve_data(a.data)
    with psycopg.connect(a.dsn) as conn:
        if a.schema:
            with open(a.schema, encoding="utf-8") as f:
                conn.execute(f.read())
        with conn.transaction():
            out = load(conn, root, a.reset)
    print(json.dumps(out, ensure_ascii=False))
    for w in out["warnings"]:
        print("경고:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
