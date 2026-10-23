"""내규 적재(/internal/documents)와 rag-mcp search_policy 현행 필터 검증. 실행 중인 두 MCP와 DB가 필요하다.

현행 내규를 새 버전으로 바꾸므로 다른 검증(verify_spec·verify_db·verify_live_rubric·verify_e2e) 뒤에, 깨끗한 DB에서 돌린다.
시드 내규 구간으로 PDF를 만들어(tests/policy_pdf.py) 올리므로 원본 PDF는 필요 없다.
    python tests/verify_documents.py [--data data]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import urllib.error
import urllib.request

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import BUSINESS, Session, check, internal, issue_run, report  # noqa: E402
from harness import error_code as code  # noqa: E402
from policy_pdf import build_pdf, policy_lines  # noqa: E402

COMPANY = "C-LGCNS-SALES"
DOC = "SALES-AI-GL-001"
CHANGED_TEXT = "① AI가 산출한 금액은 표준 가격표와 할인 정책으로 다시 계산하고, 계산 근거를 견적서 승인 요청에 남긴다."


def upload(source_file: str, data: bytes, version: str | None, **kw) -> dict:
    body = {"company_id": COMPANY, "version": version, "source_file": source_file,
            "content_base64": base64.b64encode(data).decode(), **kw}
    return internal("POST", "/internal/documents", body)


def no_key_status() -> int:
    req = urllib.request.Request(BUSINESS + "/internal/documents", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def policy_rows(conn) -> list[dict]:
    return conn.execute("SELECT doc_id, version, status, source_file, note FROM documents WHERE company_id=%s AND collection='policies' "
                        "ORDER BY version", (COMPANY,)).fetchall()


async def main(data_dir: str) -> int:
    docs = json.load(open(os.path.join(data_dir, "seed", "documents.json"), encoding="utf-8"))
    seed = next(d for d in docs if d.get("collection") == "policies" and d["doc_id"] == DOC)
    chunks = seed["chunks"]
    target = next(c for c in chunks if c["clause_id"] == "ART-19" and c["text"].startswith("①"))
    changed = [{**c, "text": CHANGED_TEXT} if c is target else c for c in chunks]
    v11_lines = policy_lines(changed)
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)

    # 1) 거절: 키 없음·형식·필수 조항. 거절된 요청은 DB를 바꾸지 않는다
    check("키 없이 호출 → 403", no_key_status() == 403)
    check("없는 회사 → NOT_FOUND", code(internal("POST", "/internal/documents", {"company_id": "NOPE"})) == "NOT_FOUND")
    check("version 없음 → VALIDATION_ERROR", code(upload("a.pdf", b"x", None)) == "VALIDATION_ERROR")
    bad64 = internal("POST", "/internal/documents", {"company_id": COMPANY, "version": "v9", "source_file": "a.pdf",
                                                     "content_base64": "%%%"})
    check("base64 아님 → VALIDATION_ERROR", code(bad64) == "VALIDATION_ERROR", str(bad64))
    check("docx → VALIDATION_ERROR", code(upload("규정.docx", b"PK", "v9")) == "VALIDATION_ERROR")
    check("조항 없는 문서 → VALIDATION_ERROR", code(upload("메모.txt", "그냥 메모입니다".encode(), "v9")) == "VALIDATION_ERROR")
    no11 = upload("규정.pdf", build_pdf(policy_lines([c for c in chunks if c["clause_id"] != "ART-11"])), "v9")
    check("ART-11 빠진 내규 → VALIDATION_ERROR(조항 이름 포함)",
          code(no11) == "VALIDATION_ERROR" and "ART-11" in no11["error"]["message"], str(no11))
    check("거절 뒤 내규는 v1.0 하나(현행)", [(r["version"], r["status"]) for r in policy_rows(conn)] == [(seed["version"], "현행")],
          str(policy_rows(conn)))

    # 2) 새 버전(PDF) 적재: 이전 현행은 폐기, 바뀐 구간만 diff
    r = upload("영업_AI활용지침_v1.1.pdf", build_pdf(v11_lines), "v1.1", uploaded_by="AD-4001")
    d = r.get("data") or {}
    check("v1.1 PDF 적재 성공", r["ok"], str(r)[:300])
    check("응답: 현행·구간 수·폐기 목록", d.get("status") == "현행" and d.get("chunks") == len(chunks)
          and d.get("retired_docs") == [f"{DOC}@{seed['version']}"] and d.get("duplicate") is False, json.dumps(d, ensure_ascii=False)[:300])
    check("응답: diff는 바뀐 구간 하나", d.get("diff") == {"base": f"{DOC}@{seed['version']}", "added": [], "removed": [],
                                                    "changed": [target["chunk_id"]]}, str(d.get("diff")))
    check("응답: rag-mcp reload 성공", d.get("rag_reloaded") is True)
    rows = {r["version"]: r for r in policy_rows(conn)}
    check("DB: v1.1 현행, v1.0 폐기", rows.get("v1.1", {}).get("status") == "현행" and rows[seed["version"]]["status"] == "폐기", str(rows))
    check("DB: 원본 파일명·업로드한 사람", rows.get("v1.1", {}).get("source_file") == "영업_AI활용지침_v1.1.pdf"
          and "AD-4001" in (rows.get("v1.1", {}).get("note") or ""))
    n = {v: conn.execute("SELECT count(*) AS n FROM document_chunks WHERE doc_id=%s AND version=%s", (DOC, v)).fetchone()["n"]
         for v in ("v1.1", seed["version"])}
    check("DB: 새 구간 적재, 이전 버전 구간 유지", n == {"v1.1": len(chunks), seed["version"]: len(chunks)}, str(n))

    # 3) Agent 경로: search_policy는 현행 한 버전만(평가 기준 생성기의 '한 버전' 조건), 검색 모드도 같다
    tok = internal("POST", "/internal/rubric-runs", {"company_id": COMPANY, "requested_by": "AD-4001"})
    async with Session(tok["data"]["run_token"]) as s:
        all_clauses = sorted({c["clause_id"] for c in chunks})
        pol = await s.call("rag", "search_policy", {"clause_ids": all_clauses})
        got = pol.get("data") or {}
        check("search_policy(clause_ids): 누락 없음", pol["ok"] and not got.get("missing_clause_ids"), str(pol)[:300])
        check("search_policy(clause_ids): v1.1만, 구간 중복 없음",
              {c["version"] for c in got.get("chunks", [])} == {"v1.1"} and len(got.get("chunks", [])) == len(chunks),
              str({(c["version"]) for c in got.get("chunks", [])}))
        hit = next((c for c in got.get("chunks", []) if c["chunk_id"] == target["chunk_id"]), {})
        check("search_policy: 바뀐 구간은 새 문장", hit.get("text") == CHANGED_TEXT, str(hit)[:200])
        q = await s.call("rag", "search_policy", {"query": "계산 근거를 견적서 승인 요청에 남긴다"})
        qv = {c["version"] for c in (q.get("data") or {}).get("chunks", [])}
        check("search_policy(query): 결과는 v1.1만", q["ok"] and qv == {"v1.1"}, str(q)[:300])

    # 4) 지난 평가가 인용한 이전 버전 구간은 get_reference로 그대로 열린다
    evs = internal("GET", "/internal/evaluations")["data"]
    run = issue_run(evs[0]["evaluation_id"])
    async with Session(run["run_token"]) as s:
        old = await s.call("rag", "get_reference", {"doc_id": DOC, "version": seed["version"], "chunk_id": target["chunk_id"]})
        od = old.get("data") or {}
        check("get_reference(이전 버전): 폐기 표시·원문 유지", old["ok"] and od.get("status") == "폐기" and od.get("text") == target["text"],
              str(old)[:300])

    # 5) 재시도·충돌: 같은 내용은 처음 결과, 같은 버전에 다른 내용은 VERSION_CONFLICT
    again = upload("영업_AI활용지침_v1.1.pdf", build_pdf(v11_lines), "v1.1")
    check("같은 버전·같은 내용 재전송 → duplicate:true", again["ok"] and again["data"]["duplicate"] is True, str(again)[:200])
    conflict = upload("규정.txt", "\n".join(policy_lines(chunks)).encode(), "v1.1")
    check("같은 버전·다른 내용 → VERSION_CONFLICT", code(conflict) == "VERSION_CONFLICT", str(conflict)[:200])

    # 6) TXT 적재: 내용이 같으면 diff는 비고, 현행만 바뀐다
    t = upload("영업_AI활용지침_v1.2.txt", "\n".join(v11_lines).encode(), "v1.2", effective_from="2026-12-01")
    td = t.get("data") or {}
    check("v1.2 TXT 적재 성공·diff 없음", t["ok"] and td.get("diff", {}).get("changed") == [] and td.get("retired_docs") == [f"{DOC}@v1.1"],
          str(t)[:300])
    current = [r["version"] for r in policy_rows(conn) if r["status"] == "현행"]
    check("DB: 회사의 현행 내규는 v1.2 하나", current == ["v1.2"], str(current))
    tok = internal("POST", "/internal/rubric-runs", {"company_id": COMPANY})
    async with Session(tok["data"]["run_token"]) as s:
        pol = await s.call("rag", "search_policy", {"clause_ids": ["ART-19"]})
        check("search_policy: v1.2로 바뀜", pol["ok"] and {c["version"] for c in pol["data"]["chunks"]} == {"v1.2"}, str(pol)[:200])

        # 7) 초안을 만든 뒤 그 초안만 인용한 조항(ART-08)이 빠진 내규가 올라오면, 적재는 되고 초안 승인은 거절된다
        cur = (await s.call("rag", "get_current_rubric", {"work_type": "quote"}))["data"]
        items = [{k: v for k, v in i.items() if k != "item_id"} for i in cur["items"]]
        items[0]["related_clauses"] = items[0]["related_clauses"] + ["ART-08"]
        dr = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": f"doc-{os.getpid()}"})
        check("ART-08을 인용한 초안 저장", dr["ok"] and dr["data"]["status"] == "초안", str(dr)[:200])
    v13 = upload("영업_AI활용지침_v1.3.txt", "\n".join(policy_lines([c for c in changed if c["clause_id"] != "ART-08"])).encode(), "v1.3")
    check("초안만 인용한 조항이 빠진 내규는 적재됨", v13["ok"], str(v13)[:200])
    ap = internal("POST", "/internal/rubrics/approve", {"company_id": COMPANY, "work_types": ["quote"]})
    check("그 초안 승인 → VALIDATION_ERROR(ART-08)", code(ap) == "VALIDATION_ERROR" and "ART-08" in ap["error"]["message"], str(ap)[:200])
    still = [(r["version"], r["status"]) for r in conn.execute(
        "SELECT version, status FROM documents WHERE company_id=%s AND collection='rubrics' AND work_type='quote' ORDER BY version",
        (COMPANY,)).fetchall()]
    check("거절된 승인은 기준 상태를 바꾸지 않음", ("초안" in {s for _, s in still}) and [v for v, s in still if s == "현행"] == [cur["version"]],
          str(still))
    dis = internal("POST", "/internal/rubrics/discard", {"company_id": COMPANY, "work_types": ["quote"]})
    check("초안 폐기", dis["ok"], str(dis)[:200])
    conn.close()
    return report()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    sys.exit(asyncio.run(main(ap.parse_args().data)))
