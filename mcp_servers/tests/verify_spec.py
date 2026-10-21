"""MCP 구현 명세 대조 검증 (서버 두 개가 떠 있는 상태에서 실행).

    INTERNAL_API_KEY=... python tests/verify_spec.py

1) 평가 건 전부(43건)에 대해 조회 도구 전체를 호출하고, 응답에서 나온 근거 ID로 초안을 저장해 본다.
2) 명세 4.2·4.3·6.2·6.3의 확인 항목을 케이스별로 검사한다.
마지막에 PASS/FAIL 집계를 출력하고 실패가 있으면 종료 코드 1.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Session, internal, issue_run  # noqa: E402

PHONE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PERSON = re.compile(r"[가-힣]OO\s?(과장|대리|차장|부장|팀장)")

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    return bool(cond)


def no_pii(o) -> bool:
    s = json.dumps(o, ensure_ascii=False)
    return not (PHONE.search(s) or EMAIL.search(s) or PERSON.search(s))


def ids_in(o, out: set):
    """응답 곳곳의 근거 ID 필드를 모은다."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("message_id", "span_id", "line_id", "link_id", "approval_id", "access_id", "evidence_id",
                     "registration_evidence_id", "ref") and isinstance(v, str):
                out.add(v)
            ids_in(v, out)
    elif isinstance(o, list):
        for x in o:
            ids_in(x, out)


async def call_retry(s: Session, server: str, tool: str, args: dict, tries: int = 4):
    """명세 2장: TEMPORARY_FAILURE는 최대 3회 재시도."""
    errs = []
    for _ in range(tries):
        r = await s.call(server, tool, args)
        if r["ok"] or r["error"]["code"] != "TEMPORARY_FAILURE":
            return r, errs
        errs.append(r["error"])
    return r, errs


# ─────────────────────────── 1) 전 건 순회 ───────────────────────────
async def sweep():
    evals = internal("GET", "/internal/evaluations")["data"]
    check("평가 건 수 = work.completed 고유 키 수(43)", len(evals) == 43, str(len(evals)))
    all_pii_ok = True
    for ev in evals:
        eid, wid, ver = ev["evaluation_id"], ev["work_id"], ev["artifact_version"]
        run = issue_run(eid)
        ids: set[str] = set()
        async with Session(run["run_token"]) as s:
            wc, _ = await call_retry(s, "business", "get_work_case", {"work_id": wid})
            ok = check(f"[{eid}] get_work_case", wc["ok"], json.dumps(wc.get("error"), ensure_ascii=False))
            if not ok:
                continue
            d = wc["data"]
            ids_in(d, ids)
            conv, _ = await call_retry(s, "business", "get_conversation", {"work_id": wid})
            check(f"[{eid}] get_conversation", conv["ok"], str(conv.get("error")))
            ids_in(conv.get("data"), ids)
            outs = [wc, conv]
            for sd in d["source_documents"]:
                r, _ = await call_retry(s, "business", "get_source_document", {"attachment_id": sd["attachment_id"]})
                check(f"[{eid}] get_source_document {sd['attachment_id']}", r["ok"], str(r.get("error")))
                ids_in(r.get("data"), ids)
                outs.append(r)
            refs = d["artifact_refs"]
            for a in refs:
                r, _ = await call_retry(s, "business", "get_artifact_version", {"artifact_id": a["artifact_id"], "version": a["version"]})
                check(f"[{eid}] get_artifact_version {a['version']}", r["ok"], str(r.get("error")))
                ids_in(r.get("data"), ids)
                outs.append(r)
            if len(refs) >= 2:
                r, _ = await call_retry(s, "business", "get_artifact_diff",
                                        {"artifact_id": refs[0]["artifact_id"], "from_version": refs[-2]["version"], "to_version": refs[-1]["version"]})
                check(f"[{eid}] get_artifact_diff", r["ok"], str(r.get("error")))
                outs.append(r)
            for tool in ("get_linked_works", "get_reference_access_log", "get_evidence_links", "get_approval_requests"):
                r, _ = await call_retry(s, "business", tool, {"work_id": wid})
                check(f"[{eid}] {tool}", r["ok"], str(r.get("error")))
                ids_in(r.get("data"), ids)
                outs.append(r)
            lw = outs[-4]["data"]["works"]
            for w in lw:
                fa = w["final_artifact"]
                if "artifact_id" in fa:
                    r, _ = await call_retry(s, "business", "get_artifact_version", fa)
                    check(f"[{eid}] 연결 업무 산출물 {fa['artifact_id']}", r["ok"], str(r.get("error")))
                    ids_in(r.get("data"), ids)
                else:
                    r, _ = await call_retry(s, "rag", "get_reference", {"doc_id": fa["doc_id"], "version": fa["version"]})
                    check(f"[{eid}] 과거 업무 확정 견적 {fa['doc_id']}", r["ok"], str(r.get("error")))
                    ids_in(r.get("data"), ids)
            r, _ = await call_retry(s, "business", "get_policy_check_result", {"work_id": wid, "version": ver})
            check(f"[{eid}] get_policy_check_result", r["ok"], str(r.get("error")))
            ids_in(r.get("data"), ids)
            outs.append(r)
            r, _ = await call_retry(s, "business", "compute_reference_quote", {"work_id": wid, "artifact_version": ver})
            if d["work_type"] == "quote":
                check(f"[{eid}] compute_reference_quote", r["ok"], str(r.get("error")))
                ids_in(r.get("data"), ids)
            else:
                check(f"[{eid}] compute_reference_quote 비견적 → VALIDATION_ERROR", not r["ok"] and r["error"]["code"] == "VALIDATION_ERROR")
            rub, errs = await call_retry(s, "rag", "search_rubric", {"work_type": d["work_type"], "rubric_version": ev["rubric_version"]})
            check(f"[{eid}] search_rubric", rub["ok"], str(rub.get("error")))
            items = rub["data"]["items"]
            clauses = sorted({c for it in items for c in it["related_clauses"]})
            r, _ = await call_retry(s, "rag", "search_policy", {"clause_ids": clauses})
            check(f"[{eid}] search_policy(clause_ids)", r["ok"] and not r["data"]["missing_clause_ids"], str(r.get("error") or r["data"].get("missing_clause_ids")))
            ids_in(r.get("data"), ids)
            for ref in d["attached_references"]:
                r, _ = await call_retry(s, "rag", "get_reference", ref)
                check(f"[{eid}] get_reference {ref['doc_id']}", r["ok"], str(r.get("error")))
                ids_in(r.get("data"), ids)
            r, _ = await call_retry(s, "rag", "search_reference", {"query": d["registration"] and json.dumps(d["registration"], ensure_ascii=False)[:80] or "제품"})
            check(f"[{eid}] search_reference", r["ok"], str(r.get("error")))
            ids_in(r.get("data"), ids)
            for o in outs:
                if not no_pii(o):
                    all_pii_ok = False
                    check(f"[{eid}] 응답에 연락처·이메일·담당자명 원문 없음", False, json.dumps(o, ensure_ascii=False)[:200])

            # 응답에서 받은 근거 ID는 모두 저장 검증을 통과해야 한다
            draft_items = []
            id_list = sorted(ids)
            for n, it in enumerate(items):
                sec = it["item"] == "보안 준수"
                draft_items.append({"item_id": it["item_id"], "item": it["item"], "status": "채점", "score": 2 if sec else 1,
                                    "reason": "검증 스크립트", "evidence_ids": id_list if n == 0 else id_list[:1]})
            key = f"{wid}:{ver}:verify:{uuid.uuid4().hex[:6]}"
            r = await s.call("business", "save_evaluation_draft",
                             {"evaluation_id": eid, "rubric_version": ev["rubric_version"], "items": draft_items, "idempotency_key": key})
            check(f"[{eid}] 응답 근거 ID {len(id_list)}개 전부로 초안 저장", r["ok"], json.dumps(r.get("error"), ensure_ascii=False)[:400])
    check("전 건 응답에 고객 연락처·이메일·담당자명 원문 없음", all_pii_ok)


# ─────────────────────────── 2) 명세 확인 항목 ───────────────────────────
EXPECTED_QUOTES = {  # 명세 4.3 단위 테스트 기준값
    "EVAL-E2E-QT-01-v3": (720_000_000, 15, 612_000_000, {("소속 팀장", "할인"), ("담당 임원", "무상 제공")}, [], None),
    "EVAL-E2E-QT-02-v2": (540_000_000, 5, 513_000_000, set(), [], None),
    "EVAL-E2E-QT-03-v2": (432_000_000, 8, 397_440_000, {("소속 팀장", "할인")}, [], None),
    "EVAL-E2E-QT-04-v2": (450_000_000, 5, 427_500_000, {("담당 임원", "SLA 상향")}, [], None),
    "EVAL-E2E-QT-05-v2": (2_178_000_000, 3, 2_112_660_000, set(), [], None),
    "EVAL-E2E-QT-06-v2": (86_400_000, 0, 86_400_000, set(), [], None),
    "EVAL-E2E-QT-07-v2": (36_000_000, 0, 36_000_000, set(), ["신규 고객 프로모션"], 34_920_000),
    "EVAL-E2E-QT-08-v2": (486_000_000, 3, 471_420_000, {("담당 임원", "무상 제공"), ("소속 팀장", "결제 조건 변경")}, [], None),
    "EVAL-E2E-QT-09-v2": (492_000_000, 2, 482_160_000, set(), [], None),
    "EVAL-E2E-QT-10-v2": (216_000_000, 0, 216_000_000, set(), [], None),
}
EXPECTED_VIOLATIONS = {  # 명세 4.2
    ("E2E-RFP-04", "V-PII-INPUT", "M01"), ("E2E-RFP-10", "V-PII-INPUT", "M01"),
    ("E2E-RFP-05", "V-CONFIDENTIAL-INPUT", "M01"), ("E2E-IQ-04", "V-CONFIDENTIAL-INPUT", "M03"),
    ("E2E-PR-01", "V-OTHER-CUSTOMER", "L04"), ("E2E-IQ-07", "V-OTHER-CUSTOMER", None),
    ("E2E-QT-10", "V-INTERNAL-PRICING", None), ("E2E-RFP-05", "V-ROADMAP", None), ("E2E-IQ-04", "V-ROADMAP", None),
}


async def with_run(eid: str, fn, **kw):
    run = issue_run(eid, **kw)
    async with Session(run["run_token"]) as s:
        return await fn(s, run)


async def spec_checks():
    evals = {e["evaluation_id"]: e for e in internal("GET", "/internal/evaluations")["data"]}

    # 4.3 기준 견적
    for eid, (sub, pct, total, targets, unrec, twu) in EXPECTED_QUOTES.items():
        async def f(s, run, eid=eid):
            return await s.call("business", "compute_reference_quote", {"work_id": run["work_id"], "artifact_version": run["snapshot"]["artifact_ref"]["version"]})
        r = await with_run(eid, f)
        d = r["data"]
        got_t = {(t["approver"], t["type"]) for t in d["approval_targets"]}
        ok = d["subtotal"] == sub and d["discount_pct"] == pct and d["total"] == total and got_t == targets and d["unrecognized_components"] == unrec
        ok = ok and d.get("total_with_unrecognized") == twu
        check(f"4.3 기준 견적 {eid}", ok, f"{d['subtotal']} {d['discount_pct']} {d['total']} {got_t} {d['unrecognized_components']} {d.get('total_with_unrecognized')}")
    r = await with_run("EVAL-E2E-QT-10-v2", lambda s, run: s.call("business", "compute_reference_quote", {"work_id": "W-E2E-QT-10", "artifact_version": "v2"}))
    w = r["data"]["warnings"]
    check("4.3 QT-10 PRICE_TABLE_MISMATCH(초안)", any(x["code"] == "PRICE_TABLE_MISMATCH" and x["status"] == "초안" and x["quoted_price_table"] == "PRICE-2026.3-DRAFT" for x in w), str(w))

    # 4.2 정책 위반 (전 평가 건)
    found = set()
    raw_free = True
    for eid, ev in evals.items():
        async def f(s, run, ev=ev):
            return await s.call("business", "get_policy_check_result", {"work_id": ev["work_id"], "version": ev["artifact_version"]})
        r = await with_run(eid, f)
        for v in r["data"]["violations"]:
            case = ev["work_id"][2:]
            found.add((case, v["rule_id"], v["location"].split(":")[-1].split("-")[-1] if v["rule_id"] in ("V-PII-INPUT", "V-CONFIDENTIAL-INPUT") else v["location"].split(":")[-1]))
            if set(v) - {"rule_id", "clause", "location", "matched_customer_id"}:
                raw_free = False
    norm_found = {(c, r, l if (c, r, l) in EXPECTED_VIOLATIONS else None) for c, r, l in found}
    check("4.2 위반 탐지 = 명세 표 (케이스·규칙)", {(c, r) for c, r, _ in norm_found} == {(c, r) for c, r, _ in EXPECTED_VIOLATIONS}, str(sorted(found)))
    check("4.2 위치 지정 위반(M01·M03·L04) 위치 일치", all((c, r, l) in found for c, r, l in EXPECTED_VIOLATIONS if l), str(sorted(found)))
    check("4.2 위반 응답에 원문 값 없음 (위치 필드만)", raw_free)

    # 6.3 고객 원문 마스킹
    async def f(s, run):
        return await s.call("business", "get_source_document", {"attachment_id": "ATT-E2E-RFP-01"})
    r = await with_run("EVAL-E2E-RFP-01-v2", f)
    txt = json.dumps(r["data"], ensure_ascii=False)
    check("6.3 RFP-01 첨부 마스킹 '[고객 담당자] ([연락처], [이메일])'", "[고객 담당자] ([연락처], [이메일])" in txt and no_pii(r["data"]), txt[-200:])
    check("6.3 RFP-01 줄 location 파싱 (3.6 (p.7))", any(l["location"] == "3.6 (p.7)" for l in r["data"]["lines"]))
    for eid in ("EVAL-E2E-RFP-04-v2", "EVAL-E2E-IQ-02-v2"):
        r = await with_run(eid, lambda s, run: s.call("business", "get_work_case", {"work_id": run["work_id"]}))
        sm = r["data"]["source_message"]
        check(f"6.3 {eid} source_message 마스킹", sm is not None and no_pii(sm) and ("[연락처]" in sm["content"] or "[이메일]" in sm["content"] or "[고객 담당자]" in sm["content"]),
              (sm or {}).get("content", "")[-160:])

    # 6.3 직원 입력 위반: 정책 결과에 기록 + 대화는 마스킹
    for eid in ("EVAL-E2E-RFP-04-v2", "EVAL-E2E-RFP-10-v2"):
        async def f(s, run):
            p = await s.call("business", "get_policy_check_result", {"work_id": run["work_id"], "version": "v2"})
            c = await s.call("business", "get_conversation", {"work_id": run["work_id"], "from_seq": 1, "to_seq": 1})
            return p, c
        p, c = await with_run(eid, f)
        m01 = c["data"]["messages"][0]
        check(f"6.3 {eid} V-PII-INPUT 기록 + M01 마스킹", any(v["rule_id"] == "V-PII-INPUT" for v in p["data"]["violations"]) and no_pii(m01) and ("[연락처]" in m01["content"] or "[이메일]" in m01["content"]),
              m01["content"][:120])

    # 6.3 고객 메일 속 지시문은 그대로 데이터로 전달
    import os
    cases = {json.loads(l)["case_id"]: json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "..", "data", "all_cases.jsonl"), encoding="utf-8")}
    for eid, cid in (("EVAL-E2E-IQ-01-v2", "E2E-IQ-01"), ("EVAL-E2E-IQ-09-v2", "E2E-IQ-09")):
        r = await with_run(eid, lambda s, run: s.call("business", "get_work_case", {"work_id": run["work_id"]}))
        raw_txt = cases[cid]["raw"]["work_case"]["source_message"]["content"]
        got = r["data"]["source_message"]["content"]
        strip = lambda t: re.sub(r"\[(연락처|이메일|고객 담당자)\]", "", PERSON.sub("", EMAIL.sub("", PHONE.sub("", t))))
        check(f"6.3 {cid} 고객 메일 지시문 원문 보존(마스킹 외 변경 없음)", strip(raw_txt) == strip(got) and r["data"]["source_message"]["content_type"] == "data")

    # 6.3 다중 버전: 스냅샷 밖 요청은 VERSION_CONFLICT
    async def f(s, run):
        a = await s.call("business", "get_conversation", {"work_id": "W-E2E-RFP-03", "from_seq": 1, "to_seq": 7})
        b = await s.call("business", "get_artifact_version", {"artifact_id": "ART-E2E-RFP-03", "version": "v3"})
        c = await s.call("business", "get_conversation", {"work_id": "W-E2E-RFP-03"})
        return a, b, c, run
    a, b, c, run = await with_run("EVAL-E2E-RFP-03-v2", f)
    check("6.3 RFP-03 v2: 범위 밖 대화 → VERSION_CONFLICT", not a["ok"] and a["error"]["code"] == "VERSION_CONFLICT")
    check("6.3 RFP-03 v2: v3 산출물 → VERSION_CONFLICT", not b["ok"] and b["error"]["code"] == "VERSION_CONFLICT")
    check("6.3 RFP-03 v2: 기본 대화 조회는 conversation_range(1~4)만", c["ok"] and [m["seq"] for m in c["data"]["messages"]] == [1, 2, 3, 4], str(run["snapshot"]["conversation_range"]))
    for eid, v in (("EVAL-E2E-QT-01-v2", "v3"), ("EVAL-E2E-PR-10-v2", "v3")):
        async def f(s, run, v=v):
            return await s.call("business", "get_artifact_version", {"artifact_id": run["snapshot"]["artifact_ref"]["artifact_id"], "version": v})
        r = await with_run(eid, f)
        check(f"6.3 {eid}: 이후 버전 {v} → VERSION_CONFLICT", not r["ok"] and r["error"]["code"] == "VERSION_CONFLICT")

    # 6.3 정정 공고: RFP-03은 같은 doc_id의 v1.0·v1.1을 각각, PR-04·IQ-05는 정정본 v1.1만 첨부돼 있다
    for eid, want in (("EVAL-E2E-RFP-03-v3", {"v1.0", "v1.1"}), ("EVAL-E2E-PR-04-v2", {"v1.1"}), ("EVAL-E2E-IQ-05-v2", {"v1.1"})):
        if eid not in evals:
            eid = sorted(k for k in evals if k.startswith(eid.rsplit("-", 1)[0]))[-1]
        async def f(s, run):
            wc = await s.call("business", "get_work_case", {"work_id": run["work_id"]})
            docs = [await s.call("business", "get_source_document", {"attachment_id": d["attachment_id"]}) for d in wc["data"]["source_documents"]]
            return docs
        docs = await with_run(eid, f)
        got = {d["data"]["version"] for d in docs if d["ok"] and d["data"]["doc_id"] == "RFP-GA-2026-02"}
        check(f"6.3 {eid} 정정 공고 원문 버전 {sorted(want)} 각각 조회", got == want, str(got))

    # 6.3 연결 업무
    async def f(s, run):
        lw = await s.call("business", "get_linked_works", {"work_id": "W-E2E-PR-01"})
        g = await s.call("rag", "get_reference", {"doc_id": "Q-2026-0412", "version": "v1.0"})
        return lw, g
    lw, g = await with_run("EVAL-E2E-PR-01-v2", f)
    hist = [w for w in lw["data"]["works"] if w["work_id"] == "W-HIST-QT-0412"]
    check("6.3 PR-01 과거 업무 evaluated:false + 확정 견적 문서 위치", hist and hist[0]["evaluated"] is False and hist[0]["final_artifact"] == {"doc_id": "Q-2026-0412", "version": "v1.0"})
    check("6.3 PR-01 과거 확정 견적 get_reference 가능", g["ok"], str(g.get("error")))
    r = await with_run("EVAL-E2E-QT-08-v2", lambda s, run: s.call("business", "get_linked_works", {"work_id": "W-E2E-QT-08"}))
    w = r["data"]["works"]
    check("6.3 QT-08 → PR-08 연결, 최신 산출물 참조", w and w[0]["evaluated"] is True and w[0]["final_artifact"]["artifact_id"] == "ART-E2E-PR-08", str(w))

    # 6.3 승인 요청 유무
    for n in range(1, 11):
        eid = sorted(k for k in evals if k.startswith(f"EVAL-E2E-QT-{n:02d}-"))[-1]
        r = await with_run(eid, lambda s, run: s.call("business", "get_approval_requests", {"work_id": run["work_id"]}))
        has = bool(r["data"]["requests"])
        check(f"6.3 QT-{n:02d} 승인 요청 {'있음' if n in (1, 3, 8) else '없음'}", has == (n in (1, 3, 8)), str(r["data"]))

    # 6.3 이의 제기·검토 결정
    async def f(s, run):
        a = await s.call("business", "get_appeal", {"evaluation_id": run["evaluation_id"]})
        d = await s.call("business", "get_review_decision", {"evaluation_id": run["evaluation_id"]})
        return a, d
    a, d = await with_run("EVAL-E2E-PR-01-v2", f, as_of="2026-10-08T12:00:00+09:00")
    check("6.3 PR-01 get_appeal cited_evidence = 스냅샷 구간 ID", a["ok"] and a["data"]["cited_evidence"] == ["ART-E2E-PR-01:v2:L04"], str(a))
    check("6.3 PR-01 결정 전(as_of 12:00) get_review_decision → NOT_FOUND", not d["ok"] and d["error"]["code"] == "NOT_FOUND")
    a, d = await with_run("EVAL-E2E-PR-10-v2", f)
    check("6.3 PR-10 이의·결정(수정) 조회", a["ok"] and d["ok"] and d["data"]["decision"] == "수정", str(d))
    r = await with_run("EVAL-E2E-QT-08-v2", lambda s, run: s.call("business", "get_appeal", {"evaluation_id": run["evaluation_id"]}))
    check("6.3 이의 없는 건 get_appeal → NOT_FOUND", not r["ok"] and r["error"]["code"] == "NOT_FOUND")

    # 6.3 범위 밖 접근
    async def f(s, run):
        return [
            await s.call("business", "get_work_case", {"work_id": "W-E2E-QT-07"}),
            await s.call("business", "get_source_document", {"attachment_id": "ATT-E2E-RFP-01"}),
            await s.call("business", "get_artifact_version", {"artifact_id": "ART-E2E-RFP-01", "version": "v2"}),
            await s.call("business", "get_appeal", {"evaluation_id": "EVAL-E2E-PR-01-v2"}),
            await s.call("rag", "get_reference", {"doc_id": "PROD-DOCAI", "version": "v3.2"}),
            await s.call("rag", "get_reference", {"doc_id": "ROADMAP-2027", "version": "v0.3"}),
        ]
    rs = await with_run("EVAL-E2E-QT-08-v2", f)
    check("6.3 다른 업무 work_id → FORBIDDEN", rs[0]["error"]["code"] == "FORBIDDEN")
    check("6.3 다른 업무 첨부 → FORBIDDEN", rs[1]["error"]["code"] == "FORBIDDEN")
    check("6.3 연결되지 않은 업무 산출물 → FORBIDDEN", rs[2]["error"]["code"] == "FORBIDDEN")
    check("6.3 다른 평가 건 → FORBIDDEN", rs[3]["error"]["code"] == "FORBIDDEN")
    check("5.2 스냅샷 밖 참조 자료 → NOT_FOUND(존재 비노출)", rs[4]["error"]["code"] == "NOT_FOUND")
    check("5.1 기밀 문서 ROADMAP-2027 → NOT_FOUND", rs[5]["error"]["code"] == "NOT_FOUND")
    async with Session("not-a-token") as s:
        r = await s.call("business", "get_work_case", {"work_id": "W-E2E-QT-08"})
        r2 = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v2"})
    check("2장 잘못된 실행 토큰 → FORBIDDEN (두 서버)", r["error"]["code"] == "FORBIDDEN" and r2["error"]["code"] == "FORBIDDEN")
    run = issue_run("EVAL-E2E-QT-08-v2")
    head, sig = run["run_token"].split(".")
    import base64
    claims = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    claims["work_id"] = "W-E2E-QT-07"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "." + sig
    async with Session(forged) as s:
        r = await s.call("business", "get_work_case", {"work_id": "W-E2E-QT-07"})
    check("2장 변조된 토큰(업무 바꿔치기) → FORBIDDEN", r["error"]["code"] == "FORBIDDEN")

    # 6.2 장애 주입
    for eid, server, tool, args, times in (
        ("EVAL-E2E-RFP-01-v2", "business", "get_conversation", {"work_id": "W-E2E-RFP-01"}, 1),
        ("EVAL-E2E-RFP-09-v2", "rag", "search_policy", {"clause_ids": ["ART-07"]}, 1),
        ("EVAL-E2E-IQ-09-v2", "rag", "search_rubric", {"work_type": "inquiry_reply", "rubric_version": "v2"}, 2),
    ):
        async def f(s, run, server=server, tool=tool, args=args):
            return await call_retry(s, server, tool, args)
        r, errs = await with_run(eid, f)
        check(f"6.2 {eid} {server}.{tool} {times}회 TEMPORARY_FAILURE(retryable) 후 복구",
              r["ok"] and len(errs) == times and all(e["code"] == "TEMPORARY_FAILURE" and e["retryable"] for e in errs), str(errs))
    async def f(s, run):
        return await s.call("business", "get_conversation", {"work_id": "W-E2E-QT-08"})
    r = await with_run("EVAL-E2E-QT-08-v2", f)
    check("6.2 장애 주입 없는 케이스는 첫 호출 성공", r["ok"])

    # 5장 rag
    async def f(s, run):
        return [
            await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v1"}),
            await s.call("rag", "search_reference", {"query": "Contact AI 상담석 단가"}),
            await s.call("rag", "search_examples", {"item": "결과 검증", "work_type": "quote", "score_level": 1}),
            await s.call("rag", "search_examples", {"item": "결과 검증", "work_type": "quote", "score_level": 2}),
            await s.call("rag", "search_policy", {"query": "로드맵 출시 일정 기밀"}),
            await s.call("rag", "search_policy", {}),
            await s.call("rag", "search_reference", {"query": "Contact AI 상담석", "doc_refs": ["PRICE-2026.2@2026.2", "PROD-DOCAI@v3.2"]}),
        ]
    rs = await with_run("EVAL-E2E-QT-10-v2", f)
    check("5.2 search_rubric 없는 버전 → NOT_FOUND", rs[0]["error"]["code"] == "NOT_FOUND")
    st = {x["doc_id"]: x["status"] for x in rs[1]["data"]["results"]}
    check("5.2 QT-10 search_reference가 초안 가격표 status를 내려줌", st.get("PRICE-2026.3-DRAFT") == "초안", str(st))
    check("5.2 search_reference 결과는 스냅샷 첨부 자료만", set(st) <= {"PRICE-2026.3-DRAFT", "PRICE-2026.2"}, str(st))
    check("5.2 search_examples 0·1점 사례 반환", rs[2]["ok"] and rs[2]["data"]["examples"] and all(e["work_type"] in ("quote", "*") for e in rs[2]["data"]["examples"]))
    check("5.2 search_examples 2점 → 빈 결과 정상", rs[3]["ok"] and rs[3]["data"]["examples"] == [] and rs[3]["data"]["no_evidence"])
    check("5.1 기밀 로드맵 내용은 검색 결과에 없음", rs[4]["ok"] and "2027년 1분기 출시" not in json.dumps(rs[4]["data"], ensure_ascii=False))
    check("5.2 search_policy 입력 없음 → VALIDATION_ERROR", rs[5]["error"]["code"] == "VALIDATION_ERROR")
    check("5.2 doc_refs로 좁히고 스냅샷 밖 문서는 무시", rs[6]["ok"] and rs[6]["data"]["ignored_doc_refs"] == ["PROD-DOCAI@v3.2"]
          and rs[6]["data"]["results"] and {x["doc_id"] for x in rs[6]["data"]["results"]} == {"PRICE-2026.2"}, str(rs[6]["data"])[:300])

    # 3.2 저장 도구 검증
    ev = evals["EVAL-E2E-QT-08-v2"]
    async def f(s, run):
        rub = (await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v2"}))["data"]["items"]
        good = []
        for it in rub:
            good.append({"item_id": it["item_id"], "item": it["item"], "status": "채점", "score": 2 if it["item"] == "보안 준수" else 1,
                         "reason": "검증", "evidence_ids": ["W-E2E-QT-08-M01"]})
        out = {}
        bad = [dict(x) for x in good]; bad[0]["item_id"] = "quote.9"
        out["wrong_ids"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad1"})
        bad = [dict(x) for x in good]; sec = next(i for i, x in enumerate(bad) if x["item"] == "보안 준수"); bad[sec]["score"] = 1
        out["sec1"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad2"})
        bad = [dict(x) for x in good]; bad[1]["evidence_ids"] = ["ART-E2E-QT-07:v2:L01"]
        out["outside"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad3"})
        bad = [dict(x) for x in good]; bad[2]["reason"] = "고객 담당자 010-1234-5678로 확인"
        out["pii"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad4"})
        bad = [dict(x) for x in good]; bad[3] = {**bad[3], "status": "미관측", "score": 1}
        out["unobs"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad5"})
        bad = [dict(x) for x in good]; bad[4] = {**bad[4], "status": "검토 필요", "score": None, "reason": None}
        out["review_noreason"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": bad, "idempotency_key": "k-bad6"})
        out["rubver"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v1", "items": good, "idempotency_key": "k-bad7"})
        out["coach_before"] = None
        k = f"W-E2E-QT-08:v2:v2:{uuid.uuid4().hex[:6]}"
        out["ok1"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": good, "idempotency_key": k})
        out["dup"] = await s.call("business", "save_evaluation_draft", {"evaluation_id": run["evaluation_id"], "rubric_version": "v2", "items": good, "idempotency_key": k})
        coach = [{"item_id": "quote.3", "kind": "보완점", "target_span_id": "ART-E2E-QT-08:v2:L09", "text": "합계를 기준 견적과 대조한 기록을 남기세요."},
                 {"item_id": "quote.1", "kind": "기록 안내", "text": "업무 등록 시 예산 항목을 비워 두지 마세요."}]
        out["coach_bad"] = await s.call("business", "save_coaching", {"evaluation_id": run["evaluation_id"], "coaching": [{**coach[0], "target_span_id": "ART-E2E-QT-08:v9:L01"}], "idempotency_key": "c-bad"})
        out["coach_ok"] = await s.call("business", "save_coaching", {"evaluation_id": run["evaluation_id"], "coaching": coach, "idempotency_key": k + ":c"})
        out["p1"] = await s.call("business", "update_progress", {"evaluation_id": run["evaluation_id"], "stage": "근거 확인 중"})
        out["p_back"] = await s.call("business", "update_progress", {"evaluation_id": run["evaluation_id"], "stage": "수집 중"})
        out["p_server"] = await s.call("business", "update_progress", {"evaluation_id": run["evaluation_id"], "stage": "확정"})
        out["rr_wrong_state"] = await s.call("business", "request_review", {"evaluation_id": run["evaluation_id"], "target_items": ["결과 검증"], "reason": "x", "review_memo": {}, "idempotency_key": "r-1"})
        return out
    internal("POST", f"/internal/evaluations/{ev['evaluation_id']}/status", {"status": "대기"})
    o = await with_run("EVAL-E2E-QT-08-v2", f)
    code = lambda r: (r.get("error") or {}).get("code")
    check("3.2 루브릭에 없는 item_id → VALIDATION_ERROR", code(o["wrong_ids"]) == "VALIDATION_ERROR")
    check("3.2 보안 준수 1점 → VALIDATION_ERROR", code(o["sec1"]) == "VALIDATION_ERROR")
    check("3.2 스냅샷 밖 근거 ID → VALIDATION_ERROR", code(o["outside"]) == "VALIDATION_ERROR")
    check("3.2 reason에 연락처 → VALIDATION_ERROR", code(o["pii"]) == "VALIDATION_ERROR")
    check("3.2 미관측에 점수 → VALIDATION_ERROR", code(o["unobs"]) == "VALIDATION_ERROR")
    check("3.2 검토 필요에 reason 없음 → VALIDATION_ERROR", code(o["review_noreason"]) == "VALIDATION_ERROR")
    check("3.2 루브릭 버전 불일치 → VERSION_CONFLICT", code(o["rubver"]) == "VERSION_CONFLICT")
    check("3.2 정상 초안 저장", o["ok1"]["ok"], str(o["ok1"].get("error")))
    check("2장 같은 idempotency_key → 기존 결과 + duplicate", o["dup"]["ok"] and o["dup"]["meta"].get("duplicate") and o["dup"]["data"] == o["ok1"]["data"])
    check("3.2 코칭 target_span_id 스냅샷 밖 → VALIDATION_ERROR", code(o["coach_bad"]) == "VALIDATION_ERROR")
    check("3.2 코칭 저장(기록 안내는 대상 구간 생략)", o["coach_ok"]["ok"], str(o["coach_ok"].get("error")))
    check("3.2 update_progress 앞으로 이동", o["p1"]["ok"])
    check("3.2 update_progress 뒤로 이동 → VALIDATION_ERROR", code(o["p_back"]) == "VALIDATION_ERROR")
    check("3.2 update_progress 서버 전용 상태 → VALIDATION_ERROR", code(o["p_server"]) == "VALIDATION_ERROR")
    check("3.2 request_review 확인 대기 아님 → VALIDATION_ERROR", code(o["rr_wrong_state"]) == "VALIDATION_ERROR")

    internal("POST", "/internal/evaluations/EVAL-E2E-PR-01-v2/status", {"status": "확인 대기"})
    async def f(s, run):
        a = await s.call("business", "get_appeal", {"evaluation_id": run["evaluation_id"]})
        bad = await s.call("business", "request_review", {"evaluation_id": run["evaluation_id"], "target_items": ["결과물 품질"], "reason": "x",
                                                          "review_memo": {"cited_evidence": []}, "idempotency_key": "rr-bad"})
        ok = await s.call("business", "request_review", {"evaluation_id": run["evaluation_id"], "target_items": a["data"]["target_items"],
                                                         "reason": "직원 이의: 공개 레퍼런스 여부 확인 필요",
                                                         "review_memo": {"cited_evidence": a["data"]["cited_evidence"] + ["W-E2E-PR-01:policy_check"],
                                                                         "comparison": "고객 마스터의 공개 승인 여부와 직원 주장이 다름"},
                                                         "idempotency_key": f"rr-{uuid.uuid4().hex[:6]}"})
        return bad, ok
    bad, ok = await with_run("EVAL-E2E-PR-01-v2", f)
    check("3.2 request_review 이의·검토 필요 외 항목 → VALIDATION_ERROR", code(bad) == "VALIDATION_ERROR")
    check("3.2 request_review 정상(검토자 서버 배정)", ok["ok"] and ok["data"]["reviewer_id"] == "R-2001" and ok["data"]["status"] == "검토 대기", str(ok))
    det = internal("GET", "/internal/evaluations/EVAL-E2E-PR-01-v2")["data"]
    check("3.2 검토 요청 후 평가 상태 '검토 대기'", det["status"] == "검토 대기")

    # 평가 건 중복 생성 방지 (RFP-01 완료 이벤트 재전송)
    rfp01 = [e for e in evals.values() if e["work_id"] == "W-E2E-RFP-01"]
    again = internal("POST", "/internal/evaluations", {"work_id": "W-E2E-RFP-01", "artifact_version": "v2", "rubric_version": "v2",
                                                        "idempotency_key": "W-E2E-RFP-01:v2:v2"})
    check("2장 RFP-01 완료 이벤트 중복 → 평가 건 1건, 재전송은 기존 건", len(rfp01) == 1 and again["data"]["created"] is False)


async def main():
    internal("POST", "/internal/faults/reset", {})
    await sweep()
    await spec_checks()
    fails = [r for r in results if not r[1]]
    for name, ok, detail in results:
        if not ok:
            print(f"FAIL  {name}  {detail}")
    print(f"\n검사 {len(results)}건: 통과 {len(results) - len(fails)}, 실패 {len(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
