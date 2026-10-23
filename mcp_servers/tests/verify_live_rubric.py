"""중간 점검(live)·평가 기준 저장(rubric) 도구 검증. 실행 중인 두 MCP와 DB가 필요하다.

현행 평가 기준을 바꾸므로 verify_spec·verify_db 뒤에, 깨끗한 DB에서 돌린다.
    python tests/verify_live_rubric.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import psycopg

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Session, call_retry, check, internal, issue_run, report  # noqa: E402
from harness import error_code as code  # noqa: E402
from harness import new_key as key  # noqa: E402

WORK, OWNER = "W-E2E-RFP-01", "E-1021"     # 메시지 6개 → 최근 5개는 2~6


def live_items(rubric: dict, *, quality_status: str = "미관측", evidence: str = f"{WORK}-M02") -> list[dict]:
    by_name = {i["item"]: i["item_id"] for i in rubric["items"]}
    return [
        {"item_id": by_name["문제 정의"], "item": "문제 정의", "status": "채점", "score": 2, "reason": "요구사항 범위를 명확히 지시",
         "evidence_ids": [evidence]},
        {"item_id": by_name["지시 품질"], "item": "지시 품질", "status": "채점", "score": 1, "reason": "출력 형식은 있으나 근거 기준 일부 누락",
         "evidence_ids": [f"{WORK}-M03"]},
        {"item_id": by_name["결과 검증"], "item": "결과 검증", "status": "검토 필요", "score": None, "reason": "원문 대조 여부가 대화에 드러나지 않음",
         "evidence_ids": []},
        {"item_id": by_name["보안 준수"], "item": "보안 준수", "status": "미관측", "score": None, "reason": None, "evidence_ids": []},
        {"item_id": by_name["결과물 품질"], "item": "결과물 품질", "status": quality_status,
         "score": 2 if quality_status == "채점" else None, "reason": None,
         "evidence_ids": [f"{WORK}-M04"] if quality_status == "채점" else []},
    ]


async def live_checks() -> None:
    lid = f"LIVE-TEST-{uuid.uuid4().hex[:6]}"
    r = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER, "live_eval_id": lid})
    check("live 요청 생성: 대기, 최근 메시지 5개(2~6)",
          r["ok"] and r["data"]["status"] == "대기" and (r["data"]["from_seq"], r["data"]["to_seq"]) == (2, 6), str(r)[:300])
    again = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER, "live_eval_id": lid})
    check("같은 live_eval_id → 기존 행(created=false)", again["ok"] and again["data"]["created"] is False, str(again)[:200])
    rl = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER})
    check("같은 사용자 5분 안 재요청 → RATE_LIMITED", code(rl) == "RATE_LIMITED", str(rl)[:200])
    other = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": "E-1034"})
    check("업무 담당자가 아닌 요청 → FORBIDDEN", code(other) == "FORBIDDEN", str(other)[:200])

    run = internal("POST", "/internal/live-runs", {"live_eval_id": lid})
    check("live 실행 토큰 발급(kind=live)", run["ok"] and run["data"]["kind"] == "live", str(run)[:200])
    async with Session(run["data"]["run_token"]) as s:
        conv = await call_retry(s, "business", "get_conversation", {"work_id": WORK})
        check("live: get_conversation 기본 범위 = 2~6",
              conv["ok"] and [m["seq"] for m in conv["data"]["messages"]] == [2, 3, 4, 5, 6], str(conv)[:200])
        out = await s.call("business", "get_conversation", {"work_id": WORK, "from_seq": 1, "to_seq": 6})
        check("live: 범위 밖 대화 → VERSION_CONFLICT", code(out) == "VERSION_CONFLICT")
        wc = await s.call("business", "get_work_case", {"work_id": WORK})
        check("live 토큰으로 get_work_case → FORBIDDEN", code(wc) == "FORBIDDEN", str(wc)[:200])
        draft = await s.call("business", "save_evaluation_draft",
                             {"evaluation_id": "EVAL-E2E-RFP-01-v2", "rubric_version": "v2", "items": [], "idempotency_key": key()})
        check("live 토큰으로 최종 평가 저장 → FORBIDDEN", code(draft) == "FORBIDDEN")
        rub = await s.call("rag", "get_current_rubric", {"work_type": "rfp_analysis"})
        check("live: rag get_current_rubric(rfp_analysis) = v2", rub["ok"] and rub["data"]["version"] == "v2", str(rub)[:200])
        rubric = rub["data"]

        st = await s.call("business", "start_live_evaluation", {"live_eval_id": lid, "idempotency_key": key()})
        polled = internal("GET", f"/internal/live-evaluations/{lid}")
        check("start_live_evaluation → 생성 중(폴링)", st["ok"] and polled["data"]["status"] == "생성 중", str(st)[:200])

        base = {"live_eval_id": lid, "work_type": "rfp_analysis", "rubric_version": "v2", "coaching": []}
        bad1 = await s.call("business", "save_live_evaluation",
                            {**base, "items": live_items(rubric, evidence=f"{WORK}-M01"), "idempotency_key": key()})
        check("범위 밖 근거(M01) → VALIDATION_ERROR", code(bad1) == "VALIDATION_ERROR", str(bad1)[:200])
        bad2 = await s.call("business", "save_live_evaluation",
                            {**base, "items": live_items(rubric, quality_status="채점"), "idempotency_key": key()})
        check("결과물 품질 채점 → VALIDATION_ERROR(산출물 없음)", code(bad2) == "VALIDATION_ERROR", str(bad2)[:200])
        bad3 = await s.call("business", "save_live_evaluation",
                            {**base, "rubric_version": "v9", "items": live_items(rubric), "idempotency_key": key()})
        check("없는 기준 버전 → NOT_FOUND", code(bad3) == "NOT_FOUND", str(bad3)[:200])
        bad4 = await s.call("business", "save_live_evaluation",
                            {**base, "items": live_items(rubric)[:4], "idempotency_key": key()})
        check("항목 4개 → VALIDATION_ERROR", code(bad4) == "VALIDATION_ERROR")

        k = key()
        coaching = [{"item_id": rubric["items"][1]["item_id"], "kind": "보완점", "target_span_id": f"{WORK}-M03",
                     "text": "대응 근거로 쓸 설명서 조항 번호를 지시에 함께 적어 보세요."}]
        ok = await s.call("business", "save_live_evaluation",
                          {**base, "items": live_items(rubric), "coaching": coaching, "idempotency_key": k, "model": "gemma4",
                           "latency_ms": 1234})
        check("save_live_evaluation 정상 → 완료", ok["ok"] and ok["data"]["status"] == "완료" and ok["data"]["items_saved"] == 5,
              str(ok)[:300])
        dup = await s.call("business", "save_live_evaluation",
                           {**base, "items": live_items(rubric), "coaching": coaching, "idempotency_key": k})
        check("같은 idempotency_key → 기존 결과(duplicate)", dup["ok"] and dup["meta"].get("duplicate") and dup["data"] == ok["data"])
        later = await s.call("business", "save_live_evaluation", {**base, "items": live_items(rubric), "idempotency_key": key()})
        check("완료 뒤 다시 저장 → VALIDATION_ERROR", code(later) == "VALIDATION_ERROR")
        late_fail = await s.call("business", "fail_live_evaluation", {"live_eval_id": lid, "error": "x", "idempotency_key": key()})
        check("완료 뒤 실패 처리 → VALIDATION_ERROR", code(late_fail) == "VALIDATION_ERROR")
        alien = await s.call("business", "start_live_evaluation", {"live_eval_id": "LIVE-NOT-MINE", "idempotency_key": key()})
        check("다른 중간 점검 ID → FORBIDDEN", code(alien) == "FORBIDDEN")

    det = internal("GET", f"/internal/live-evaluations/{lid}")["data"]
    check("저장 결과: 완료·기준 RUB-rfp_analysis v2·항목 5·코칭 1",
          det["status"] == "완료" and det["rubric_doc_id"] == "RUB-rfp_analysis" and det["rubric_version"] == "v2"
          and len(det["items"]) == 5 and len(det["coaching"]) == 1 and det["model"] == "gemma4", json.dumps(det, ensure_ascii=False)[:300])
    again_run = internal("POST", "/internal/live-runs", {"live_eval_id": lid})
    check("완료된 중간 점검은 토큰 재발급 안 됨", code(again_run) == "VALIDATION_ERROR")

    # 실패 경로: 다른 직원의 업무
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        wid2, owner2 = conn.execute(
            "SELECT w.work_id, w.employee_id FROM work_cases w WHERE w.employee_id <> %s AND EXISTS "
            "(SELECT 1 FROM messages m WHERE m.work_id = w.work_id) ORDER BY w.work_id LIMIT 1", (OWNER,)).fetchone()
    r2 = internal("POST", "/internal/live-evaluations", {"work_id": wid2, "requested_by": owner2})
    run2 = internal("POST", "/internal/live-runs", {"live_eval_id": r2["data"]["live_eval_id"]})["data"]
    async with Session(run2["run_token"]) as s:
        f = await s.call("business", "fail_live_evaluation",
                         {"live_eval_id": r2["data"]["live_eval_id"], "error": "LLM 서버 응답 시간 초과", "idempotency_key": key()})
    det2 = internal("GET", f"/internal/live-evaluations/{r2['data']['live_eval_id']}")["data"]
    check("fail_live_evaluation → 실패·사유 저장", f["ok"] and det2["status"] == "실패" and det2["error"] == "LLM 서버 응답 시간 초과",
          str(det2)[:200])


def rubric_payload(rubric: dict, criterion_suffix: str = "") -> list[dict]:
    return [{k: v for k, v in i.items() if k != "item_id"} | ({"criterion": i["criterion"] + criterion_suffix} if criterion_suffix else {})
            for i in rubric["items"]]


async def rubric_checks() -> None:
    tok = internal("POST", "/internal/rubric-runs", {"company_id": "C-LGCNS-SALES", "requested_by": "AD-4001"})
    check("평가 기준 작성 토큰 발급(kind=rubric)", tok["ok"] and tok["data"]["kind"] == "rubric", str(tok)[:200])
    bad_company = internal("POST", "/internal/rubric-runs", {"company_id": "NOPE"})
    check("없는 회사 → NOT_FOUND", code(bad_company) == "NOT_FOUND")
    async with Session(tok["data"]["run_token"]) as s:
        cur = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("rubric: get_current_rubric(quote) = v2", cur["ok"] and cur["data"]["version"] == "v2", str(cur)[:200])
        pol = await s.call("rag", "search_policy", {"clause_ids": ["ART-11"]})
        check("rubric 토큰으로 search_policy 가능", pol["ok"], str(pol)[:200])
        conv = await s.call("business", "get_conversation", {"work_id": WORK})
        check("rubric 토큰으로 get_conversation → FORBIDDEN", code(conv) == "FORBIDDEN")

        items = rubric_payload(cur["data"], " (기준 생성 Agent 수정)")
        cases = {
            "항목 4개": items[:4],
            "현행 내규에 없는 조항": [items[0] | {"related_clauses": ["ART-99"]}] + items[1:],
            "보안 준수 score_1 ≠ 해당 없음": items[:3] + [items[3] | {"score_1": "일부 준수"}] + items[4:],
            "항목 순서 바뀜": [items[1], items[0]] + items[2:],
        }
        for name, bad in cases.items():
            r = await s.call("business", "save_rubric", {"work_type": "quote", "items": bad, "idempotency_key": key()})
            check(f"save_rubric {name} → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
        stale = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key(),
                                                         "based_on_version": "v1"})
        check("save_rubric based_on_version이 현행 아님 → VERSION_CONFLICT", code(stale) == "VERSION_CONFLICT", str(stale)[:200])

        k = key()
        ok = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": k, "based_on_version": "v2"})
        check("save_rubric 정상 → RUB-quote v3 현행, v2 폐기",
              ok["ok"] and ok["data"]["version"] == "v3" and ok["data"]["retired_versions"] == ["v2"], str(ok)[:300])
        dup = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": k, "based_on_version": "v2"})
        check("같은 idempotency_key → 기존 결과(duplicate)", dup["ok"] and dup["meta"].get("duplicate") and dup["data"] == ok["data"])
        cur2 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("저장 직후 rag get_current_rubric = v3(수정된 기준)",
              cur2["ok"] and cur2["data"]["version"] == "v3" and cur2["data"]["items"][0]["criterion"].endswith("(기준 생성 Agent 수정)"),
              str(cur2)[:200])
        old = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v2"})
        check("이전 버전 v2는 폐기 상태로 조회 가능", old["ok"] and old["data"]["status"] == "폐기", str(old)[:200])
        again = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key(),
                                                         "based_on_version": "v2"})
        check("이미 바뀐 기준 위에 다시 저장 → VERSION_CONFLICT", code(again) == "VERSION_CONFLICT")

    run = issue_run("EVAL-E2E-QT-01-v2")
    async with Session(run["run_token"]) as s:
        w = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key()})
        check("최종 평가 토큰으로 save_rubric → FORBIDDEN", code(w) == "FORBIDDEN")
        pinned = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": run["rubric_version"]})
        check("기존 평가 건은 자기 기준 버전(v2)을 그대로 조회", pinned["ok"] and pinned["data"]["version"] == "v2", str(pinned)[:200])

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute("SELECT version, status FROM documents WHERE doc_id='RUB-quote' ORDER BY version").fetchall()
        n = conn.execute("SELECT count(*) FROM rubric_items WHERE doc_id='RUB-quote' AND version='v3'").fetchone()[0]
    check("PG documents·rubric_items 반영", rows == [("v2", "폐기"), ("v3", "현행")] and n == 5, str(rows))


async def main() -> int:
    await live_checks()
    await rubric_checks()
    return report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
