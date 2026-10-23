"""중간 피드백(live, 계약 docs/recent_five_feedback.md)·평가 기준 저장(rubric) 도구 검증. 실행 중인 두 MCP와 DB가 필요하다.

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

WORK, OWNER = "W-E2E-RFP-01", "E-1021"     # u a u a u a → 완료 3쌍, 범위 1~6
GAP_WORK, GAP_OWNER = "W-E2E-QT-01", "E-1047"   # u a u a u u a u → 답 없는 5·8은 빠지고 쌍은 1-2·3-4·6-7
COMPANY = "C-LGCNS-SALES"
# Agent 계약 모델(docs/recent_five_feedback.md, extra 금지)과 같은 칸
CONTEXT_KEYS = {"live_eval_id", "company_id", "work_id", "requested_by", "requested_at", "from_seq", "to_seq", "status",
                "work_type", "rubric_doc_id", "rubric_version", "rubric"}
BATCH_KEYS = {"live_eval_id", "work_id", "from_seq", "to_seq", "masking_applied", "masking_version", "complete", "messages"}
MESSAGE_KEYS = {"message_id", "exchange_id", "seq", "role", "content", "created_at"}
RECEIPT_KEYS = {"live_eval_id", "status", "duplicate"}


def feedback(lid: str, batch: dict, criterion_id: str | None) -> dict:
    """Agent가 save_live_evaluation에 보내는 모양(개선 1 + 강점 1, 추천 1)."""
    q = next(m for m in batch["messages"] if m["role"] == "user")
    a = next(m for m in batch["messages"] if m["role"] == "assistant")
    items = [
        {"item_id": "F-1", "criterion_id": criterion_id, "kind": "improvement", "diagnosis": "요청에 근거로 쓸 자료와 출력 형식이 빠져 있습니다.",
         "evidence": [{"message_id": q["message_id"], "quote": q["content"][:20]}]},
        {"item_id": "F-2", "criterion_id": None, "kind": "strength", "diagnosis": "AI 답변을 바로 이어서 활용했습니다.",
         "evidence": [{"message_id": a["message_id"], "quote": a["content"][:15]}]},
    ]
    coaching = {"schema_version": "1", "provenance": {"trace_id": "trace-verify", "prompt_version": "recent-five-feedback-v1"},
                "suggestions": [{"item_id": "F-1", "target_message_id": q["message_id"], "next_action": "근거 자료와 표 형식을 함께 지정하세요.",
                                 "suggested_prompt": "[자료명] 기준으로 요구사항을 표로 정리해 줘. 열은 번호·요구사항·근거 위치야."}]}
    return {"live_eval_id": lid, "status": "완료", "items": items, "coaching": coaching, "model": "gemma4", "latency_ms": 1234,
            "error": None}


async def live_checks() -> None:
    lid = f"LIVE-TEST-{uuid.uuid4().hex[:6]}"
    r = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER, "live_eval_id": lid})
    d = r.get("data") or {}
    check("요청 생성: 대기, 완료 3쌍 → 범위 1~6, 업무 유형·현행 기준 고정",
          r["ok"] and d["status"] == "대기" and (d["from_seq"], d["to_seq"]) == (1, 6) and d["work_type"] == "rfp_analysis"
          and (d["rubric_doc_id"], d["rubric_version"]) == ("RUB-rfp_analysis", "v2"), str(r)[:300])
    again = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER, "live_eval_id": lid})
    check("같은 live_eval_id → 기존 행(created=false)", again["ok"] and again["data"]["created"] is False, str(again)[:200])
    rl = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": OWNER})
    check("같은 사용자 5분 안 재요청 → RATE_LIMITED", code(rl) == "RATE_LIMITED", str(rl)[:200])
    other = internal("POST", "/internal/live-evaluations", {"work_id": WORK, "requested_by": "E-1034"})
    check("업무 담당자가 아닌 요청 → FORBIDDEN", code(other) == "FORBIDDEN", str(other)[:200])
    bad_type = internal("POST", "/internal/live-evaluations", {"work_id": GAP_WORK, "requested_by": GAP_OWNER, "work_type": "memo"})
    check("모르는 work_type → VALIDATION_ERROR", code(bad_type) == "VALIDATION_ERROR", str(bad_type)[:200])

    tok = internal("POST", "/internal/live-worker-token", {"company_id": COMPANY})
    check("Worker 토큰 발급(kind=live, 회사 범위, 요청 ID 없음)",
          tok["ok"] and tok["data"]["kind"] == "live" and tok["data"]["company_id"] == COMPANY and "live_eval_id" not in tok["data"],
          str(tok)[:200])
    check("없는 회사 Worker 토큰 → NOT_FOUND", code(internal("POST", "/internal/live-worker-token", {"company_id": "NOPE"})) == "NOT_FOUND")
    async with Session(tok["data"]["run_token"]) as s:
        for tool, args in (("get_conversation", {"work_id": WORK}), ("get_work_case", {"work_id": WORK}),
                           ("save_evaluation_draft", {"evaluation_id": "EVAL-E2E-RFP-01-v2", "rubric_version": "v2", "items": [],
                                                      "idempotency_key": key()})):
            r = await s.call("business", tool, args)
            check(f"Worker 토큰으로 최종 평가 도구 {tool} → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:160])
        early = await s.call("business", "get_messages", {"live_eval_id": lid, "work_id": WORK, "from_seq": 1, "to_seq": 6})
        check("start 전 get_messages → VALIDATION_ERROR", code(early) == "VALIDATION_ERROR", str(early)[:200])

        st = await call_retry(s, "business", "start_live_evaluation", {"live_eval_id": lid})
        ctx = st.get("data") or {}
        check("start: 계약 칸 그대로(모르는 칸 없음)", st["ok"] and set(ctx) == CONTEXT_KEYS, str(sorted(ctx))[:200])
        check("start: 생성 중, 요청자·회사·범위·고정 기준(5항목 {item_id, item, criterion})",
              st["ok"] and ctx["status"] == "생성 중" and ctx["company_id"] == COMPANY and ctx["requested_by"] == OWNER
              and (ctx["from_seq"], ctx["to_seq"]) == (1, 6) and ctx["rubric"]["version"] == ctx["rubric_version"] == "v2"
              and len(ctx["rubric"]["items"]) == 5 and all(set(i) == {"item_id", "item", "criterion"} for i in ctx["rubric"]["items"]),
              json.dumps(ctx, ensure_ascii=False)[:300])
        polled = internal("GET", f"/internal/live-evaluations/{lid}")
        check("start → 폴링 상태 생성 중", polled["data"]["status"] == "생성 중")
        resume = await call_retry(s, "business", "start_live_evaluation", {"live_eval_id": lid})
        check("생성 중 재전달 → 같은 문맥", resume["ok"] and resume["data"] == ctx)

        wrong = await s.call("business", "get_messages", {"live_eval_id": lid, "work_id": WORK, "from_seq": 2, "to_seq": 6})
        check("고정 범위와 다른 get_messages → VALIDATION_ERROR", code(wrong) == "VALIDATION_ERROR", str(wrong)[:200])
        gm = await call_retry(s, "business", "get_messages", {"live_eval_id": lid, "work_id": WORK, "from_seq": 1, "to_seq": 6})
        b = gm.get("data") or {}
        msgs = b.get("messages") or []
        check("get_messages: 계약 칸 그대로", gm["ok"] and set(b) == BATCH_KEYS and all(set(m) == MESSAGE_KEYS for m in msgs),
              str(sorted(b))[:200])
        check("get_messages: 6개, 질문·답변 짝(같은 exchange_id), 완료·마스킹 표시",
              [m["role"] for m in msgs] == ["user", "assistant"] * 3 and b["complete"] is True and b["masking_applied"] is True
              and b["masking_version"].startswith("mask-")
              and all(msgs[i]["exchange_id"] == msgs[i + 1]["exchange_id"] for i in range(0, 6, 2)), json.dumps(msgs, ensure_ascii=False)[:300])

        crit = ctx["rubric"]["items"][1]["item_id"]
        good = feedback(lid, b, crit)
        q_id = good["items"][0]["evidence"][0]["message_id"]
        a_id = good["items"][1]["evidence"][0]["message_id"]
        cases = [
            ("기존 5항목 점수 형식 → VALIDATION_ERROR",
             {**good, "items": [{"item_id": crit, "item": "지시 품질", "status": "채점", "score": 1, "reason": "x", "evidence_ids": [q_id]}]}),
            ("범위 밖 메시지 인용 → VALIDATION_ERROR",
             {**good, "items": [dict(good["items"][0], evidence=[{"message_id": f"{WORK}-M99", "quote": "x"}]), good["items"][1]]}),
            ("메시지에 없는 문장 인용 → VALIDATION_ERROR",
             {**good, "items": [dict(good["items"][0], evidence=[{"message_id": q_id, "quote": "대화에 없는 문장"}]), good["items"][1]]}),
            ("고정 기준(rfp_analysis)이 아닌 항목(quote.1) → VALIDATION_ERROR",
             {**good, "items": [dict(good["items"][0], criterion_id="quote.1"), good["items"][1]]}),
            ("개선 항목에 추천 없음 → VALIDATION_ERROR",
             {**good, "coaching": {**good["coaching"], "suggestions": [dict(good["coaching"]["suggestions"][0], item_id="F-2")]}}),
            ("추천 대상이 AI 답변 → VALIDATION_ERROR",
             {**good, "coaching": {**good["coaching"], "suggestions": [dict(good["coaching"]["suggestions"][0], target_message_id=a_id)]}}),
            ("항목에 모르는 칸 → VALIDATION_ERROR", {**good, "items": [dict(good["items"][0], score=1), good["items"][1]]}),
            ("완료에 error → VALIDATION_ERROR", {**good, "error": "MODEL_TIMEOUT"}),
        ]
        for name, payload in cases:
            r = await s.call("business", "save_live_evaluation", payload)
            check(name, code(r) == "VALIDATION_ERROR", str(r)[:200])

        ok = await call_retry(s, "business", "save_live_evaluation", good)
        check("save: 완료 → {live_eval_id, status, duplicate:false}",
              ok["ok"] and ok["data"] == {"live_eval_id": lid, "status": "완료", "duplicate": False}, str(ok)[:300])
        dup = await call_retry(s, "business", "save_live_evaluation", good)
        check("같은 저장 재전달 → duplicate:true, 완료", dup["ok"] and dup["data"] == {"live_eval_id": lid, "status": "완료", "duplicate": True},
              str(dup)[:200])
        late_fail = await call_retry(s, "business", "save_live_evaluation",
                                     {"live_eval_id": lid, "status": "실패", "error": "MODEL_TIMEOUT"})
        check("완료 뒤 실패 저장 → 덮어쓰지 않고 완료·duplicate", late_fail["ok"] and late_fail["data"]["status"] == "완료"
              and late_fail["data"]["duplicate"] is True, str(late_fail)[:200])
        restart = await call_retry(s, "business", "start_live_evaluation", {"live_eval_id": lid})
        check("완료 뒤 start 재전달 → 오류 없이 현재 상태(완료) 문맥", restart["ok"] and restart["data"]["status"] == "완료", str(restart)[:200])
        missing = await s.call("business", "start_live_evaluation", {"live_eval_id": "LIVE-NO-SUCH"})
        check("없는 요청 → NOT_FOUND", code(missing) == "NOT_FOUND")
        for k in ("start_live_evaluation", "save_live_evaluation"):
            r = await s.call("business", k, {"live_eval_id": lid, **({"status": "완료"} if k == "save_live_evaluation" else {})})
            check(f"{k} 응답에 meta.duplicate·DUPLICATE 키 없음(상태로 판단)", r["ok"] and "duplicate" not in r.get("meta", {}), str(r)[:160])

    other_tok = internal("POST", "/internal/live-worker-token", {"company_id": "C-OTHER-01"})["data"]["run_token"]
    async with Session(other_tok) as s:
        r = await s.call("business", "start_live_evaluation", {"live_eval_id": lid})
        check("다른 회사 Worker 토큰 → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])

    det = internal("GET", f"/internal/live-evaluations/{lid}")["data"]
    check("저장 결과: 완료·기준 고정 유지·진단 2·추천 1·provenance 보존",
          det["status"] == "완료" and det["rubric_version"] == "v2" and len(det["items"]) == 2
          and len(det["coaching"]["suggestions"]) == 1 and det["coaching"]["provenance"]["trace_id"] == "trace-verify"
          and det["model"] == "gemma4" and det["error"] is None, json.dumps(det, ensure_ascii=False)[:300])

    # 답 없는 질문이 섞인 업무 + 유형 직접 지정 + 실패 저장
    r2 = internal("POST", "/internal/live-evaluations", {"work_id": GAP_WORK, "requested_by": GAP_OWNER, "work_type": "proposal"})
    d2 = r2.get("data") or {}
    check("유형을 직접 고르면 그 유형의 현행 기준을 고정(proposal)",
          r2["ok"] and d2["work_type"] == "proposal" and d2["rubric_doc_id"] == "RUB-proposal", str(r2)[:300])
    check("답 없는 질문은 쌍이 아님 → 범위 1~7", (d2.get("from_seq"), d2.get("to_seq")) == (1, 7), str(d2)[:200])
    async with Session(tok["data"]["run_token"]) as s:
        await call_retry(s, "business", "start_live_evaluation", {"live_eval_id": d2["live_eval_id"]})
        gm2 = await call_retry(s, "business", "get_messages",
                               {"live_eval_id": d2["live_eval_id"], "work_id": GAP_WORK, "from_seq": 1, "to_seq": 7})
        seqs = [m["seq"] for m in (gm2.get("data") or {}).get("messages", [])]
        check("get_messages: 답 없는 질문(seq 5) 빼고 1·2·3·4·6·7, complete", seqs == [1, 2, 3, 4, 6, 7]
              and gm2["data"]["complete"] is True, str(seqs))
        bad = await s.call("business", "save_live_evaluation",
                           {"live_eval_id": d2["live_eval_id"], "status": "실패", "error": "LLM 서버 응답 시간 초과"})
        check("실패 error는 코드만(문장 거부) → VALIDATION_ERROR", code(bad) == "VALIDATION_ERROR", str(bad)[:200])
        f = await call_retry(s, "business", "save_live_evaluation",
                             {"live_eval_id": d2["live_eval_id"], "status": "실패", "error": "MODEL_TIMEOUT", "latency_ms": 60000})
        check("실패 저장 → {status:실패, duplicate:false}", f["ok"] and f["data"] == {"live_eval_id": d2["live_eval_id"],
                                                                                "status": "실패", "duplicate": False}, str(f)[:200])
    det2 = internal("GET", f"/internal/live-evaluations/{d2['live_eval_id']}")["data"]
    check("실패 상태·코드 저장", det2["status"] == "실패" and det2["error"] == "MODEL_TIMEOUT", str(det2)[:200])


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

        # 초안 저장: 현행(v2)은 그대로이고 초안 v3가 생긴다
        k = key()
        ok = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": k, "based_on_version": "v2"})
        check("save_rubric 정상 → RUB-quote v3 초안(기반 v2)",
              ok["ok"] and ok["data"]["version"] == "v3" and ok["data"]["status"] == "초안" and ok["data"]["based_on_version"] == "v2"
              and ok["data"]["replaced_draft"] is False, str(ok)[:300])
        dup = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": k, "based_on_version": "v2"})
        check("같은 idempotency_key → 기존 결과(duplicate)", dup["ok"] and dup["meta"].get("duplicate") and dup["data"] == ok["data"])
        cur2 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("초안 저장 뒤에도 get_current_rubric = v2(승인 전엔 평가에 안 씀)", cur2["ok"] and cur2["data"]["version"] == "v2", str(cur2)[:200])
        draft = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v3"})
        check("초안 v3는 search_rubric으로 초안 상태로 조회",
              draft["ok"] and draft["data"]["status"] == "초안" and draft["data"]["items"][0]["criterion"].endswith("(기준 생성 Agent 수정)"),
              str(draft)[:200])

        # 다시 설계하면 열린 초안을 같은 버전으로 덮어쓴다
        items2 = rubric_payload(cur["data"], " (재설계)")
        redo = await s.call("business", "save_rubric", {"work_type": "quote", "items": items2, "idempotency_key": key()})
        check("초안이 있을 때 save_rubric → 같은 v3 덮어씀(replaced_draft)",
              redo["ok"] and redo["data"]["version"] == "v3" and redo["data"]["replaced_draft"] is True, str(redo)[:300])
        draft2 = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v3"})
        check("덮어쓴 초안 내용이 반영됨", draft2["ok"] and draft2["data"]["items"][0]["criterion"].endswith("(재설계)"), str(draft2)[:200])

    # 관리자 승인(백엔드 내부 API)
    check("승인: 없는 회사 → NOT_FOUND", code(internal("POST", "/internal/rubrics/approve", {"company_id": "NOPE"})) == "NOT_FOUND")
    check("승인: 초안 없는 유형 지정 → NOT_FOUND",
          code(internal("POST", "/internal/rubrics/approve", {"company_id": "C-LGCNS-SALES", "work_types": ["proposal"]})) == "NOT_FOUND")
    check("승인: 잘못된 유형 → VALIDATION_ERROR",
          code(internal("POST", "/internal/rubrics/approve", {"company_id": "C-LGCNS-SALES", "work_types": ["nope"]})) == "VALIDATION_ERROR")
    ap = internal("POST", "/internal/rubrics/approve", {"company_id": "C-LGCNS-SALES", "approved_by": "AD-4001"})
    check("승인 → v3 현행, v2 폐기",
          ap["ok"] and ap["data"]["approved"] == [{"work_type": "quote", "doc_id": "RUB-quote", "version": "v3", "retired_versions": ["v2"]}],
          str(ap)[:300])
    check("승인할 초안이 더 없으면 NOT_FOUND", code(internal("POST", "/internal/rubrics/approve", {"company_id": "C-LGCNS-SALES"})) == "NOT_FOUND")

    async with Session(tok["data"]["run_token"]) as s:
        cur3 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("승인 뒤 get_current_rubric = v3(재설계 기준)",
              cur3["ok"] and cur3["data"]["version"] == "v3" and cur3["data"]["items"][0]["criterion"].endswith("(재설계)"), str(cur3)[:200])
        old = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v2"})
        check("이전 버전 v2는 폐기 상태로 조회 가능", old["ok"] and old["data"]["status"] == "폐기", str(old)[:200])
        again = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key(),
                                                         "based_on_version": "v2"})
        check("이미 바뀐 기준 위에 다시 저장 → VERSION_CONFLICT", code(again) == "VERSION_CONFLICT")

        # 초안 폐기: 초안 v4를 만들고 지운다. 현행 v3는 그대로
        d4 = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key(), "based_on_version": "v3"})
        check("새 초안 v4(기반 v3)", d4["ok"] and d4["data"]["version"] == "v4" and d4["data"]["based_on_version"] == "v3", str(d4)[:200])
        dis = internal("POST", "/internal/rubrics/discard", {"company_id": "C-LGCNS-SALES", "work_types": ["quote"]})
        check("초안 폐기 → v4 삭제", dis["ok"] and dis["data"]["discarded"] == [{"work_type": "quote", "doc_id": "RUB-quote", "version": "v4"}],
              str(dis)[:200])
        gone = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v4"})
        check("폐기한 초안은 Chroma에서도 없음(NOT_FOUND)", code(gone) == "NOT_FOUND", str(gone)[:200])
        cur4 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("폐기 뒤에도 현행은 v3", cur4["ok"] and cur4["data"]["version"] == "v3", str(cur4)[:200])
    check("폐기할 초안이 없으면 NOT_FOUND", code(internal("POST", "/internal/rubrics/discard", {"company_id": "C-LGCNS-SALES"})) == "NOT_FOUND")

    # 관리자 '수정 초안 만들기'(현행 복사) → '항목 저장'(항목 하나의 칸 일부) → 승인
    co = "C-LGCNS-SALES"
    check("수정 초안: 없는 회사 → NOT_FOUND", code(internal("POST", "/internal/rubrics/drafts", {"company_id": "NOPE"})) == "NOT_FOUND")
    check("수정 초안: 잘못된 유형 → VALIDATION_ERROR",
          code(internal("POST", "/internal/rubrics/drafts", {"company_id": co, "work_types": ["nope"]})) == "VALIDATION_ERROR")
    fk = internal("POST", "/internal/rubrics/drafts", {"company_id": co, "work_types": ["quote"], "created_by": "AD-4001"})
    check("수정 초안 만들기 → v4(현행 v3 복사)",
          fk["ok"] and fk["data"]["created"] == [{"work_type": "quote", "doc_id": "RUB-quote", "version": "v4", "based_on_version": "v3"}],
          str(fk)[:300])
    check("초안이 있는 유형에 다시 만들기 → VERSION_CONFLICT",
          code(internal("POST", "/internal/rubrics/drafts", {"company_id": co, "work_types": ["quote"]})) == "VERSION_CONFLICT")
    check("전체 유형 만들기도 한 유형에 초안이 있으면 VERSION_CONFLICT",
          code(internal("POST", "/internal/rubrics/drafts", {"company_id": co})) == "VERSION_CONFLICT")

    def edit(**body) -> dict:
        return internal("POST", "/internal/rubrics/drafts/items", {"company_id": co, "work_type": "quote", **body})

    check("항목 저장: 초안 없는 유형 → NOT_FOUND",
          code(internal("POST", "/internal/rubrics/drafts/items", {"company_id": co, "work_type": "proposal", "item": "결과 검증",
                                                                   "fields": {"criterion": "x"}})) == "NOT_FOUND")
    bad_edits = {
        "없는 항목 이름": {"item": "속도", "fields": {"criterion": "x"}},
        "고칠 수 없는 칸(item_id)": {"item": "결과 검증", "fields": {"item_id": "quote.9"}},
        "평가 질문 비움": {"item": "결과 검증", "fields": {"criterion": " "}},
        "보안 준수 score_1 바꿈": {"item": "보안 준수", "fields": {"score_1": "일부 준수"}},
        "현행 내규에 없는 조항": {"item": "결과 검증", "fields": {"related_clauses": ["ART-99"]}},
        "근거 비움": {"item": "결과 검증", "fields": {"evidence_sources": []}},
    }
    for name, body in bad_edits.items():
        check(f"항목 저장 {name} → VALIDATION_ERROR", code(edit(**body)) == "VALIDATION_ERROR")

    async with Session(tok["data"]["run_token"]) as s:
        v3 = (await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v3"}))["data"]
        v4 = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v4"})
        check("수정 초안 v4는 초안 상태이고 항목은 v3와 같음",
              v4["ok"] and v4["data"]["status"] == "초안" and v4["data"]["items"] == v3["items"], str(v4)[:200])
        target = next(i for i in v3["items"] if i["item"] == "결과 검증")
        new_criterion = target["criterion"] + " (관리자 수정)"
        ed = edit(item="결과 검증", edited_by="AD-4001",
                  fields={"criterion": new_criterion, "evidence_sources": target["evidence_sources"] + ["관리자 추가 근거"]})
        check("항목 저장 → 바뀐 칸만 보고, item_id 유지",
              ed["ok"] and ed["data"]["version"] == "v4" and ed["data"]["changed_fields"] == ["criterion", "evidence_sources"]
              and ed["data"]["item"]["item_id"] == target["item_id"], str(ed)[:300])
        v4b = (await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": "v4"}))["data"]
        others_same = [i for i in v4b["items"] if i["item"] != "결과 검증"] == [i for i in v3["items"] if i["item"] != "결과 검증"]
        edited = next(i for i in v4b["items"] if i["item"] == "결과 검증")
        check("Chroma 초안에 그 항목만 반영", others_same and edited["criterion"] == new_criterion
              and edited["evidence_sources"][-1] == "관리자 추가 근거", str(edited)[:200])
        cur5 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("항목 저장 뒤에도 현행은 v3", cur5["ok"] and cur5["data"]["version"] == "v3", str(cur5)[:200])
    ap2 = internal("POST", "/internal/rubrics/approve", {"company_id": co, "approved_by": "AD-4001"})
    check("관리자 수정 초안 승인 → v4 현행, v3 폐기",
          ap2["ok"] and ap2["data"]["approved"] == [{"work_type": "quote", "doc_id": "RUB-quote", "version": "v4", "retired_versions": ["v3"]}],
          str(ap2)[:300])
    async with Session(tok["data"]["run_token"]) as s:
        cur6 = await s.call("rag", "get_current_rubric", {"work_type": "quote"})
        check("승인 뒤 현행 v4에 관리자 수정이 반영",
              cur6["ok"] and cur6["data"]["version"] == "v4"
              and next(i for i in cur6["data"]["items"] if i["item"] == "결과 검증")["criterion"] == new_criterion, str(cur6)[:200])

    run = issue_run("EVAL-E2E-QT-01-v2")
    async with Session(run["run_token"]) as s:
        w = await s.call("business", "save_rubric", {"work_type": "quote", "items": items, "idempotency_key": key()})
        check("최종 평가 토큰으로 save_rubric → FORBIDDEN", code(w) == "FORBIDDEN")
        pinned = await s.call("rag", "search_rubric", {"work_type": "quote", "rubric_version": run["rubric_version"]})
        check("기존 평가 건은 자기 기준 버전(v2)을 그대로 조회", pinned["ok"] and pinned["data"]["version"] == "v2", str(pinned)[:200])

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute("SELECT version, status FROM documents WHERE doc_id='RUB-quote' ORDER BY version").fetchall()
        n = conn.execute("SELECT count(*) FROM rubric_items WHERE doc_id='RUB-quote' AND version='v4'").fetchone()[0]
        drafts = conn.execute("SELECT count(*) FROM documents WHERE collection='rubrics' AND status='초안'").fetchone()[0]
    check("PG documents·rubric_items 반영", rows == [("v2", "폐기"), ("v3", "폐기"), ("v4", "현행")] and n == 5, str(rows))
    check("남은 초안 없음(VERSION_CONFLICT로 거절된 전체 만들기는 아무것도 만들지 않음)", drafts == 0, str(drafts))


async def main() -> int:
    await live_checks()
    await rubric_checks()
    return report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
