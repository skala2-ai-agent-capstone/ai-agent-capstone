"""DB 적재·저장 검증 (PostgreSQL + ChromaDB). verify_spec.py를 돌린 뒤 실행하면 저장 결과까지 확인한다.

    DATABASE_URL=... CHROMA_HOST=... python tests/verify_db.py [--data data]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.chroma import COLLECTIONS, collection_name, connect  # noqa: E402

PII = re.compile(r"01[016789]-?\d{3,4}-?\d{4}|[\w.+-]+@[\w-]+(\.[\w-]+)+|[가-힣]OO\s?(과장|대리|차장|부장|팀장)")
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    a = ap.parse_args()
    cases = [json.loads(l) for l in open(os.path.join(a.data, "all_cases.jsonl"), encoding="utf-8")]
    docs = json.load(open(os.path.join(a.data, "seed", "documents.json"), encoding="utf-8"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    one = lambda sql, p=(): conn.execute(sql, p).fetchone()
    cnt = lambda t: one(f"SELECT count(*) AS n FROM {t}")["n"]

    # 1) 원본 데이터와 행 수 일치
    exp = {
        "work_cases": len(cases),
        "messages": sum(len(c["raw"]["messages"]) for c in cases),
        "artifact_versions": sum(len(c["raw"]["artifact_versions"]) for c in cases),
        "artifact_spans": sum(len(v["spans"]) for c in cases for v in c["raw"]["artifact_versions"]),
        "source_documents": sum(len(c["raw"]["source_documents"]) for c in cases),
        "work_events": sum(len(c["raw"]["events"]) for c in cases),
        "human_actions": sum(len(c["raw"]["human_actions"]) for c in cases),
        "evidence_links": sum(len(c["raw"]["evidence_links"]) for c in cases),
        "approval_requests": sum(len(c["raw"].get("approval_requests") or []) for c in cases),
        "quote_data": sum(len(c["raw"].get("quote_data") or {}) for c in cases),
        "fault_injections": sum(len(c["raw"].get("fault_injection") or []) for c in cases),
        "documents": len(docs),
        "document_chunks": sum(len(d.get("chunks") or []) for d in docs),
        "rubric_items": sum(len(d.get("items") or []) for d in docs),
        "evaluations": len({e["idempotency_key"] for c in cases for e in c["raw"]["events"] if e["event_type"] == "work.completed"}),
    }
    for t, n in exp.items():
        got = cnt(t)
        check(f"PG {t} 행 수 = 원본 {n}", got == n, str(got))

    # 2) 마스킹본에는 민감정보가 없고 원문은 보존 (정책 검사용)
    masked_cols = [("messages", "content_masked"), ("source_documents", "content_masked"), ("source_messages", "content_masked"),
                   ("artifact_versions", "content_masked"), ("artifact_spans", "text_masked"),
                   ("work_cases", "registration_masked::text"), ("human_actions", "payload_masked::text")]
    for t, col in masked_cols:
        bad = [r["v"] for r in conn.execute(f"SELECT {col} AS v FROM {t}") if r["v"] and PII.search(r["v"])]
        check(f"PG {t}.{col.split('::')[0]} 마스킹본에 민감정보 없음", not bad, str(bad[:1])[:160])
    raw = one("SELECT content FROM source_documents WHERE attachment_id='ATT-E2E-RFP-01'")
    check("PG 원문 컬럼은 마스킹 전 그대로 (정책 검사용)", raw and PII.search(raw["content"]))
    m = one("SELECT content_masked FROM source_documents WHERE attachment_id='ATT-E2E-RFP-01'")
    check("PG 마스킹 치환 '[고객 담당자] ([연락처], [이메일])'", "[고객 담당자] ([연락처], [이메일])" in m["content_masked"])
    check("PG 기밀 문서는 indexed=false·collection 없음",
          one("SELECT count(*) AS n FROM documents WHERE NOT indexed AND collection IS NULL")["n"] == sum(1 for d in docs if not d["indexed"]))

    # 3) Chroma 컬렉션
    client = connect(wait_sec=10)
    secret_ids = {d["doc_id"] for d in docs if not d["indexed"]}
    want = {"references": sum(len(d["chunks"]) for d in docs if d.get("collection") == "references" and d["indexed"]),
            "policies": sum(len(d["chunks"]) for d in docs if d.get("collection") == "policies"),
            "rubrics": sum(len(d["items"]) for d in docs if d.get("collection") == "rubrics"),
            "coaching_examples": sum(1 for d in docs if d.get("collection") == "coaching_examples")}
    for k in COLLECTIONS:
        col = client.get_collection(collection_name(k), embedding_function=None)
        g = col.get(include=["metadatas"])
        check(f"Chroma {collection_name(k)} 건수 = {want[k]}", len(g["ids"]) == want[k], str(len(g["ids"])))
        leaked = [i for i, md in zip(g["ids"], g["metadatas"]) if md.get("doc_id") in secret_ids]
        check(f"Chroma {collection_name(k)}에 기밀 문서 없음", not leaked, str(leaked[:3]))
        if k in ("references", "policies"):
            pg_ids = {f"{r['doc_id']}@{r['version']}#{r['chunk_id']}" for r in conn.execute(
                "SELECT c.doc_id, c.version, c.chunk_id FROM document_chunks c JOIN documents d USING (doc_id, version) WHERE d.collection=%s", (k,))}
            check(f"Chroma {collection_name(k)} ID = PG 구간 ID ({{doc_id}}@{{version}}#{{chunk_id}})", set(g["ids"]) == pg_ids)

    # 4) MCP가 쓴 결과 (verify_spec.py 실행 후)
    n_drafts = cnt("evaluation_drafts")
    check("PG evaluation_drafts에 초안 저장됨", n_drafts >= exp["evaluations"], str(n_drafts))
    check("PG evaluation_coaching 저장됨", cnt("evaluation_coaching") >= 1)
    check("PG review_requests 저장·검토자 배정(담당 팀장)", one("SELECT count(*) AS n FROM review_requests WHERE reviewer_id='L-3001'")["n"] >= 1)
    check("PG progress_log 기록", cnt("progress_log") >= 1)
    dup = one("SELECT count(*) AS n FROM (SELECT tool, scope, key FROM idempotency_keys GROUP BY 1,2,3 HAVING count(*)>1) x")["n"]
    check("PG idempotency_keys 중복 없음", dup == 0 and cnt("idempotency_keys") >= 1)
    audit_bad = [r["input"] for r in conn.execute("SELECT input::text AS input FROM audit_log") if PII.search(r["input"])]
    check("PG audit_log 기록 + 민감정보 없음", cnt("audit_log") > 0 and not audit_bad, str(audit_bad[:1]))
    codes = {r["result_code"] for r in conn.execute("SELECT DISTINCT result_code FROM audit_log")}
    check("PG audit_log 결과 코드 기록 (OK·오류·DUPLICATE_REQUEST)", {"OK", "DUPLICATE_REQUEST", "VALIDATION_ERROR", "FORBIDDEN"} <= codes, str(codes))

    fails = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  {detail}"))
    print(f"\n검사 {len(results)}건: 통과 {len(results) - len(fails)}, 실패 {len(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
