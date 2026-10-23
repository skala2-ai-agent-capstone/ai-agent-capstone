"""운영 흐름 종단 검증: 업무 공간 대화(llm-proxy) → 업무 DB(llm_exchanges·messages 뷰) → MCP(중간 점검·최종 평가).

시드 파일에 없는 새 업무를 백엔드가 하듯 DB에 만들고, 대화는 실제 llm-proxy로 보낸다. 산출물은 본문만 저장하고
구간(artifact_spans)은 만들지 않는다(앱이 저장하는 모양). 그래서 아래 두 가지를 함께 본다.
- MCP가 파일 없이 DB만으로 돌아가는가
- 앱이 쓰는 경로로 들어온 데이터만으로 중간 점검과 최종 평가 도구가 모두 동작하는가
업스트림은 실제 vLLM(EC2, --profile llm)이나 tests/mock_llm.py(로컬, --profile mockllm) 어느 쪽이든 된다.
만든 업무는 끝나면 지운다(--keep이면 남김).

    DATABASE_URL=… INTERNAL_API_KEY=… PROXY_KEYS=이름:키 [PROXY_URL=http://localhost:4000] python tests/verify_e2e.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from business_mcp.spans import split_spans  # noqa: E402
from db.loader.chat_log import WORKSPACE_PROMPT  # noqa: E402
from harness import Session, check, internal, new_key, report  # noqa: E402
from harness import error_code as code  # noqa: E402

PROXY = os.environ.get("PROXY_URL", "http://localhost:4000").rstrip("/")
MODEL = os.environ.get("SERVED_MODEL_NAME", "gemma4")
LLM_WAIT_SEC = int(os.environ.get("E2E_LLM_WAIT_SEC", "120"))   # EC2 첫 기동은 모델 로딩에 10분 넘게 걸린다
PHONE, EMAIL = "010-2345-6789", "kim.buyer@example.com"


def proxy_key() -> tuple[str, str]:
    if os.environ.get("PROXY_KEY"):
        return os.environ.get("PROXY_MEMBER", ""), os.environ["PROXY_KEY"]
    first = (os.environ.get("PROXY_KEYS") or "").split(",")[0]
    name, _, k = first.partition(":")
    if not k:
        sys.exit("PROXY_KEYS(이름:키) 또는 PROXY_KEY가 필요합니다")
    return name.strip(), k.strip()


MEMBER, KEY = proxy_key()


def http(method: str, url: str, body=None, headers=None, timeout: float = 600) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, str(e)


def sse_content(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            for ch in json.loads(line[6:]).get("choices") or []:
                out.append((ch.get("delta") or {}).get("content") or "")
    return "".join(out)


def chat(messages: list[dict], *, work: str | None, user: str | None, stage: str | None = "workspace_chat",
         stream: bool = False, model: str = MODEL, auth: str | None = None) -> tuple[int, str | None, str]:
    headers = {"Authorization": f"Bearer {auth or KEY}"}
    for h, v in (("X-Stage", stage), ("X-Work-Id", work), ("X-User-Id", user)):
        if v:
            headers[h] = v
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 96}
    if stream:
        body.update(stream=True, stream_options={"include_usage": True})
    status, text = http("POST", PROXY + "/v1/chat/completions", body, headers)
    if status != 200:
        return status, None, text
    answer = sse_content(text) if stream else (json.loads(text)["choices"][0]["message"].get("content") or "")
    return status, answer, text


class Scenario:
    def __init__(self, conn):
        self.db = conn
        self.one = lambda sql, p=(): conn.execute(sql, p).fetchone()
        self.all = lambda sql, p=(): conn.execute(sql, p).fetchall()
        tag = uuid.uuid4().hex[:6].upper()
        self.work = f"W-E2E-APP-{tag}"
        self.artifact = f"ART-E2E-APP-{tag}"
        self.questions: list[str] = []
        self.answers: list[str] = []

    # ── 1) 백엔드 역할: 「새 업무 작성」 ──
    def create_work(self) -> None:
        u = self.one("SELECT u.user_id, u.company_id, u.team_id FROM users u WHERE u.role='member' AND u.team_id IS NOT NULL "
                     "AND NOT EXISTS (SELECT 1 FROM live_evaluations l WHERE l.requested_by=u.user_id "
                     "                AND l.requested_at > now() - interval '5 minutes') "
                     "AND (SELECT count(*) FROM users o WHERE o.company_id=u.company_id AND o.role='member') >= 2 "
                     "AND (SELECT count(*) FROM customers c WHERE c.company_id=u.company_id) >= 2 "
                     "ORDER BY u.user_id IN ('E-1021','E-1052'), u.user_id DESC LIMIT 1")
        if not check("준비: 중간 점검 5분 제한에 걸리지 않은 담당 직원 있음", u):
            raise SystemExit(report())
        self.owner, self.company, self.team = u["user_id"], u["company_id"], u["team_id"]
        other = self.one("SELECT user_id FROM users WHERE company_id=%s AND user_id<>%s AND role='member' ORDER BY user_id LIMIT 1",
                         (self.company, self.owner))
        self.other_user = other["user_id"]
        custs = self.all("SELECT customer_id, name, approved_reference FROM customers WHERE company_id=%s ORDER BY customer_id",
                         (self.company,))
        self.customer = custs[0]["customer_id"]
        rival = next(c for c in custs if c["customer_id"] != self.customer and not c["approved_reference"])
        self.rival_id, self.rival_name = rival["customer_id"], re.sub(r"\(가상\)$", "", rival["name"]).strip()
        reg = {"rfp": "RFP-APP-2026 v1.0", "customer_purpose": "계약서 검토 자동화", "constraints": f"고객 담당 {PHONE}로 일정 협의"}
        self.db.execute(
            "INSERT INTO work_cases (work_id, company_id, team_id, employee_id, work_type, customer_id, title, status, created_at, "
            "registration, registration_masked) VALUES (%s,%s,%s,%s,NULL,%s,%s,'in_progress',now(),%s,mask_text(%s)::jsonb)",
            (self.work, self.company, self.team, self.owner, self.customer, "계약서 검토 자동화 제안", Jsonb(reg),
             json.dumps(reg, ensure_ascii=False)))
        check("새 업무 생성 (work_type 미분류, 등록 마스킹본은 mask_text로)", self.one("SELECT 1 FROM work_cases WHERE work_id=%s", (self.work,)))

    def span_rule(self) -> None:
        """앱 산출물에 쓰는 구간 규칙이 적재 데이터의 구간과 같은지 (근거 ID 형식이 시드와 운영에서 같아야 한다)."""
        bad, n = [], 0
        for v in self.all("SELECT artifact_id, version, content FROM artifact_versions a WHERE EXISTS "
                          "(SELECT 1 FROM artifact_spans s WHERE s.artifact_id=a.artifact_id AND s.version=a.version)"):
            n += 1
            want = [(r["span_id"], r["text"]) for r in self.all(
                "SELECT span_id, text FROM artifact_spans WHERE artifact_id=%s AND version=%s ORDER BY seq", (v["artifact_id"], v["version"]))]
            if [(s["span_id"], s["text"]) for s in split_spans(v["artifact_id"], v["version"], v["content"])] != want:
                bad.append(f"{v['artifact_id']}@{v['version']}")
        check(f"구간 규칙 = 적재된 구간 ({n}개 버전)", n and not bad, str(bad[:3]))

    # ── 2) 프록시와 업스트림 준비 확인 ──
    def wait_llm(self) -> bool:
        deadline = time.time() + LLM_WAIT_SEC
        status, detail = 0, ""
        while time.time() < deadline:
            status, _, detail = chat([{"role": "user", "content": "ping"}], work=self.work, user=None, stage="classify")
            if status == 200:
                break
            time.sleep(5)
        return check(f"llm-proxy → 업스트림 응답 ({PROXY})", status == 200, f"{status} {detail[:200]}")

    # ── 3) 업무 공간 대화 ──
    def conversation(self) -> None:
        self.start_id = self.one("SELECT coalesce(max(id), 0) AS n FROM llm_exchanges")["n"]
        history: list[dict] = []
        turns = [
            ("계약서 검토 자동화 제안서 목차를 잡아 줘. 고객 요구는 첨부 RFP 3장 기준이야.", False),
            (f"고객 담당자 연락처는 {PHONE}, 메일은 {EMAIL} 이야. 일정 협의 메일 초안도 써 줘.", True),
            ("보안 요구사항 대응 문단을 PROD-DOCAI v3.2 §4.1 근거로 써 줘.", False),
            ("가격 부분은 빼고 기술 부분만 2쪽으로 줄여 줘.", True),
            ("마지막으로 요약 3줄 만들어 줘.", False),
            ("구축 일정을 3단계 표로 정리해 줘.", True),
        ]
        for n, (q, stream) in enumerate(turns, 1):
            msgs = [{"role": "system", "content": WORKSPACE_PROMPT}, *history, {"role": "user", "content": q}]
            if n == 3:   # 화면에서 모델 설정이 잘못돼 한 번 실패한 뒤 같은 질문을 다시 보낸다
                status, _, text = chat(msgs, work=self.work, user=self.owner, model="no-such-model")
                check("업스트림 오류(없는 모델) → 오류 그대로 전달", status >= 400, f"{status} {text[:120]}")
            status, answer, text = chat(msgs, work=self.work, user=self.owner, stream=stream)
            if not check(f"대화 {n}턴 ({'스트리밍' if stream else '일반'}) 200", status == 200 and answer is not None, f"{status} {text[:200]}"):
                return
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": answer}]
            self.questions.append(q)
            self.answers.append(answer)
        status, _, _ = chat([{"role": "system", "content": "업무 유형을 분류한다."}, {"role": "user", "content": "제안서 작성 업무"}],
                            work=self.work, user=None, stage="classify")
        check("분류 단계 호출(X-Stage: classify) 200", status == 200)

    def header_checks(self) -> None:
        msgs = [{"role": "user", "content": "헤더 확인"}]
        s, _, t = chat(msgs, work="W-NO-SUCH-WORK", user=self.owner)
        check("없는 X-Work-Id → 400 invalid_trace_headers", s == 400 and "invalid_trace_headers" in t, f"{s} {t[:150]}")
        s, _, t = chat(msgs, work=self.work, user=None)
        check("workspace_chat에 X-User-Id 없음 → 400", s == 400 and "invalid_trace_headers" in t, f"{s} {t[:150]}")
        s, _, t = chat(msgs, work=self.work, user=self.owner, stage="chat")
        check("모르는 X-Stage → 400", s == 400, f"{s} {t[:150]}")
        s, _, t = chat(msgs, work=self.work, user=self.owner, auth="wrong-key")
        check("팀원 키가 틀림 → 401", s == 401, f"{s} {t[:150]}")

    # ── 4) DB: llm_exchanges와 messages 뷰 ──
    def db_checks(self) -> None:
        rows = self.all("SELECT stage, status_code, user_id, member, error FROM llm_exchanges WHERE work_id=%s AND id>%s ORDER BY id",
                        (self.work, self.start_id))
        chat_rows = [r for r in rows if r["stage"] == "workspace_chat"]
        check("llm_exchanges: 대화 7건(성공 6 + 실패 1) 기록", len(chat_rows) == 7 and sum(r["status_code"] == 200 for r in chat_rows) == 6,
              str([(r["status_code"], r["error"]) for r in chat_rows]))
        check("llm_exchanges: 실패 호출은 error 기록", any(r["status_code"] >= 400 and r["error"] for r in chat_rows))
        check("llm_exchanges: user_id·member(팀원 키 이름) 기록",
              all(r["user_id"] == self.owner and (not MEMBER or r["member"] == MEMBER) for r in chat_rows),
              str({(r["user_id"], r["member"]) for r in chat_rows}))
        check("llm_exchanges: 분류 호출은 stage=classify, user_id 없음",
              any(r["stage"] == "classify" and r["user_id"] is None and r["status_code"] == 200 for r in rows))
        check("헤더 오류 요청은 기록하지 않음", not self.one("SELECT 1 FROM llm_exchanges WHERE work_id=%s AND stage='workspace_chat' "
                                                      "AND request->'messages'->0->>'content'='헤더 확인'", (self.work,)))

        msgs = self.all("SELECT message_id, seq, role, content, content_masked, created_at, exchange_id FROM messages WHERE work_id=%s "
                        "ORDER BY seq",
                        (self.work,))
        self.messages = msgs
        check("messages 뷰: 12개 (질문 6 + 답변 6, 실패 뒤 재전송 질문은 한 번)", len(msgs) == 12, str(len(msgs)))
        check("messages 뷰: seq 1..12, ID {work_id}-Mnn",
              [m["seq"] for m in msgs] == list(range(1, 13)) and all(m["message_id"] == f"{self.work}-M{m['seq']:02d}" for m in msgs))
        check("messages 뷰: user/assistant 번갈아", [m["role"] for m in msgs] == ["user", "assistant"] * 6)
        check("messages 뷰: 질문 원문 = 보낸 질문", [m["content"] for m in msgs if m["role"] == "user"] == self.questions)
        check("messages 뷰: 답변 = 프록시가 돌려준 답변 (스트리밍 포함)",
              [m["content"] for m in msgs if m["role"] == "assistant"] == [a or None for a in self.answers])
        check("messages 뷰: 시각 순서", all(a["created_at"] <= b["created_at"] for a, b in zip(msgs, msgs[1:])))
        pii = msgs[2] if len(msgs) > 2 else {"content": "", "content_masked": ""}
        check("messages 뷰: 마스킹본에 연락처·이메일 없음, 원문은 보존",
              PHONE not in pii["content_masked"] and EMAIL not in pii["content_masked"] and PHONE in pii["content"],
              pii["content_masked"][:120])

    # ── 5) 중간 피드백 (Worker 토큰, 계약 docs/recent_five_feedback.md) ──
    def live_before_chat(self) -> None:
        r = internal("POST", "/internal/live-evaluations", {"work_id": self.work, "requested_by": self.owner})
        check("대화 전 중간 피드백 요청 → VALIDATION_ERROR(완료 질문·응답 없음)", code(r) == "VALIDATION_ERROR", str(r)[:200])

    async def live(self) -> None:
        r = internal("POST", "/internal/live-evaluations", {"work_id": self.work, "requested_by": self.other_user})
        check("중간 피드백: 담당자가 아니면 FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])
        r = internal("POST", "/internal/live-evaluations", {"work_id": self.work, "requested_by": self.owner})
        d = r.get("data") or {}
        # 완료 6쌍 중 최근 5쌍 = 3~12. 미분류 업무라 기준 없이 일반 코칭
        if not check("요청: 대기, 최근 완료 5쌍 → 범위 3~12, 유형 미분류 → 기준 없음",
                     r.get("ok") and d["status"] == "대기" and (d["from_seq"], d["to_seq"]) == (3, 12) and d["work_type"] is None
                     and d["rubric_doc_id"] is None, str(r)[:300]):
            return
        lid = d["live_eval_id"]
        check("중간 피드백 5분 안 재요청 → RATE_LIMITED", code(internal("POST", "/internal/live-evaluations",
                                                                  {"work_id": self.work, "requested_by": self.owner})) == "RATE_LIMITED")
        tok = internal("POST", "/internal/live-worker-token", {"company_id": self.company})
        if not check("Worker 토큰 발급", tok.get("ok") and tok["data"]["kind"] == "live", str(tok)[:200]):
            return
        view = {m["message_id"]: m for m in self.messages}
        async with Session(tok["data"]["run_token"]) as s:
            st = await s.call("business", "start_live_evaluation", {"live_eval_id": lid})
            ctx = st.get("data") or {}
            check("start: 생성 중, 요청자·회사·범위, 기준 없음(rubric null)",
                  st.get("ok") and ctx["status"] == "생성 중" and ctx["requested_by"] == self.owner and ctx["company_id"] == self.company
                  and (ctx["from_seq"], ctx["to_seq"]) == (3, 12) and ctx["rubric"] is None, str(st)[:300])
            gm = await s.call("business", "get_messages", {"live_eval_id": lid, "work_id": self.work, "from_seq": 3, "to_seq": 12})
            b = gm.get("data") or {}
            msgs = b.get("messages") or []
            ids = [f"{self.work}-M{n:02d}" for n in range(3, 13)]
            check("get_messages: M03~M12 10개, complete, 본문 = messages 뷰 마스킹본",
                  [m["message_id"] for m in msgs] == ids and b.get("complete") is True
                  and all(m["content"] == view[m["message_id"]]["content_masked"] for m in msgs), str(gm)[:300])
            check("get_messages: 직원이 입력한 연락처는 마스킹", msgs and PHONE not in msgs[0]["content"])
            q3, a3 = f"{self.work}-M05", f"{self.work}-M06"   # 실패 뒤 재전송한 질문과 그 답
            pair = {m["message_id"]: m["exchange_id"] for m in msgs}
            check("실패 뒤 재전송한 질문: 뷰에서는 호출이 달라도 쌍의 exchange_id는 답한 호출로 같음",
                  view[q3]["exchange_id"] != view[a3]["exchange_id"] and pair.get(q3) == pair.get(a3) == view[a3]["exchange_id"],
                  str({k: (view[k]["exchange_id"], pair.get(k)) for k in (q3, a3)}))
            q = msgs[0]
            good = {"live_eval_id": lid, "status": "완료", "model": MODEL, "latency_ms": 1500, "error": None,
                    "items": [{"item_id": "F-1", "criterion_id": None, "kind": "improvement",
                               "diagnosis": "고객 연락처를 대화에 그대로 입력했습니다.",
                               "evidence": [{"message_id": q["message_id"], "quote": q["content"][:12]}]}],
                    "coaching": {"schema_version": "1", "provenance": {"trace_id": "e2e"},
                                 "suggestions": [{"item_id": "F-1", "target_message_id": q["message_id"],
                                                  "next_action": "연락처 대신 [고객 담당자]처럼 대체 표기를 쓰세요.",
                                                  "suggested_prompt": "[고객 담당자]와 일정을 협의하는 메일 초안을 써 줘."}]}}
            r = await s.call("business", "save_live_evaluation",
                             {**good, "items": [dict(good["items"][0], criterion_id="proposal.1")]})
            check("기준 없는 요청에 criterion_id → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
            r = await s.call("business", "save_live_evaluation", good)
            check("save → 완료, duplicate:false", r.get("ok") and r["data"] == {"live_eval_id": lid, "status": "완료", "duplicate": False},
                  str(r)[:200])
            r = await s.call("business", "save_live_evaluation", good)
            check("큐 재전달 save → duplicate:true", r.get("ok") and r["data"]["duplicate"] is True, str(r)[:200])
        got = internal("GET", f"/internal/live-evaluations/{lid}")
        check("중간 피드백 결과 조회(폴링): 완료·진단 1·추천 1",
              got.get("ok") and got["data"]["status"] == "완료" and len(got["data"]["items"]) == 1
              and len(got["data"]["coaching"]["suggestions"]) == 1, str(got)[:200])

    # ── 6) 최종 평가: 분류 → 산출물 저장(구간 없음) → 완료 이벤트 → 평가 ──
    def save_artifacts(self) -> None:
        self.db.execute("UPDATE work_cases SET work_type='proposal' WHERE work_id=%s", (self.work,))
        v1 = "# 계약서 검토 자동화 제안 (요약)\n\n1. 사업 이해: 계약서 검토 시간을 줄이는 것이 목표입니다.\n2. 구축 일정: 착수 후 16주."
        v2 = (f"# 계약서 검토 자동화 제안 (요약)\n\n1. 사업 이해: 계약서 검토 시간을 줄이는 것이 목표입니다.\n"
              f"2. 구축 일정: 착수 후 12주, 3단계로 진행합니다.\n3. 참고 사례: {self.rival_name} 구축 경험을 바탕으로 합니다.\n"
              f"4. 문의: 영업 담당 {PHONE}")
        for ver, author, content in (("v1", "ai_draft", v1), ("v2", "employee", v2)):
            time.sleep(1.1)
            self.db.execute("INSERT INTO artifact_versions (artifact_id, version, work_id, author, saved_at, content, content_masked) "
                            "VALUES (%s,%s,%s,%s,now(),%s,mask_text(%s))", (self.artifact, ver, self.work, author, content, content))
        self.v2 = v2
        self.rubric_version = self.one("SELECT version FROM documents WHERE collection='rubrics' AND work_type='proposal' AND status='현행' "
                                       "AND company_id=%s ORDER BY length(version) DESC, version DESC LIMIT 1", (self.company,))["version"]
        ikey = f"{self.work}:v2:{self.rubric_version}"
        self.db.execute("UPDATE work_cases SET status='completed', completed_at=now() WHERE work_id=%s", (self.work,))
        self.db.execute("INSERT INTO work_events (event_id, work_id, event_type, artifact_version, rubric_version, idempotency_key, occurred_at) "
                        "VALUES (%s,%s,'work.completed','v2',%s,%s,now())", (f"EVT-{uuid.uuid4().hex[:8]}", self.work, self.rubric_version, ikey))
        check("산출물은 본문만 저장 (artifact_spans 행 없음)",
              not self.one("SELECT 1 FROM artifact_spans WHERE artifact_id=%s", (self.artifact,)))
        self.ikey = ikey

    async def final(self) -> None:
        body = {"work_id": self.work, "artifact_version": "v2", "rubric_version": self.rubric_version, "idempotency_key": self.ikey}
        r = internal("POST", "/internal/evaluations", body)
        if not check("평가 건 생성 (work.completed)", r.get("ok") and r["data"]["created"], str(r)[:200]):
            return
        eid = r["data"]["evaluation_id"]
        check("같은 이벤트 재수신 → 기존 평가 건", internal("POST", "/internal/evaluations", body).get("data", {}).get("created") is False)
        run = internal("POST", "/internal/runs", {"evaluation_id": eid})
        if not check("평가 실행 토큰 발급", run.get("ok"), str(run)[:200]):
            return
        snap = run["data"]["snapshot"]
        check("스냅샷: 평가 대상 v2(직원 저장본), 대화 1~12",
              snap["artifact_ref"]["version"] == "v2" and snap["conversation_range"] == {"from_seq": 1, "to_seq": 12}, json.dumps(snap)[:200])
        want_spans = split_spans(self.artifact, "v2", self.v2)
        rival_span = next(sp["span_id"] for sp in want_spans if self.rival_name in sp["text"])
        msg3 = f"{self.work}-M03"
        async with Session(run["data"]["run_token"]) as s:
            wc = await s.call("business", "get_work_case", {"work_id": self.work})
            d = wc.get("data") or {}
            check("get_work_case: 분류 결과·산출물 2버전·대상 v2",
                  wc.get("ok") and d["work_type"] == "proposal" and [a["version"] for a in d["artifact_refs"]] == ["v1", "v2"]
                  and [a["is_evaluation_target"] for a in d["artifact_refs"]] == [False, True], str(wc)[:300])
            check("get_work_case: 등록 내용은 마스킹본", wc.get("ok") and PHONE not in json.dumps(d.get("registration"), ensure_ascii=False))
            conv = await s.call("business", "get_conversation", {"work_id": self.work})
            cm = (conv.get("data") or {}).get("messages", [])
            check("get_conversation: 12개, 연락처는 마스킹", len(cm) == 12 and PHONE not in cm[2]["content"], str(conv)[:200])
            av = await s.call("business", "get_artifact_version", {"artifact_id": self.artifact, "version": "v2"})
            spans = (av.get("data") or {}).get("spans", [])
            check("get_artifact_version: 구간을 본문에서 나눔 (시드와 같은 규칙)",
                  [sp["span_id"] for sp in spans] == [sp["span_id"] for sp in want_spans], str([sp["span_id"] for sp in spans]))
            check("get_artifact_version: 구간 텍스트도 마스킹본", spans and all(PHONE not in sp["text"] for sp in spans)
                  and PHONE not in av["data"]["content"])
            diff = await s.call("business", "get_artifact_diff", {"artifact_id": self.artifact, "from_version": "v1", "to_version": "v2"})
            types = sorted({c["type"] for c in (diff.get("data") or {}).get("changes", [])})
            check("get_artifact_diff: AI 초안 대비 수정·추가 감지", "added" in types and "modified" in types, str(diff)[:300])
            pol = await s.call("business", "get_policy_check_result", {"work_id": self.work, "version": "v2"})
            v = {(x["rule_id"], x["location"]) for x in (pol.get("data") or {}).get("violations", [])}
            check("정책 검사: 직원이 입력한 연락처 → V-PII-INPUT @M03", ("V-PII-INPUT", msg3) in v, str(v))
            check("정책 검사: 산출물에 다른 고객명 → V-OTHER-CUSTOMER @나눈 구간 ID", ("V-OTHER-CUSTOMER", rival_span) in v, str(v))
            rub = await s.call("rag", "search_rubric", {"work_type": "proposal", "rubric_version": self.rubric_version})
            if not check("search_rubric(proposal, 평가 건 기준 버전)", rub.get("ok"), str(rub)[:200]):
                return
            r = await s.call("business", "update_progress", {"evaluation_id": eid, "stage": "수집 중"})
            check("update_progress 수집 중", r.get("ok"), str(r)[:200])
            by = {i["item"]: i["item_id"] for i in rub["data"]["items"]}
            items = [
                {"item_id": by["문제 정의"], "item": "문제 정의", "status": "미관측", "score": None, "reason": None, "evidence_ids": []},
                {"item_id": by["지시 품질"], "item": "지시 품질", "status": "채점", "score": 1, "reason": "범위는 정했으나 근거 지정 부족",
                 "evidence_ids": [f"{self.work}-M01"]},
                {"item_id": by["결과 검증"], "item": "결과 검증", "status": "미관측", "score": None, "reason": None, "evidence_ids": []},
                {"item_id": by["보안 준수"], "item": "보안 준수", "status": "채점", "score": 0, "reason": "고객 연락처 입력, 다른 고객명 노출",
                 "evidence_ids": [msg3, rival_span]},
                {"item_id": by["결과물 품질"], "item": "결과물 품질", "status": "채점", "score": 1, "reason": "일정은 있으나 근거 없음",
                 "evidence_ids": [spans[2]["span_id"] if len(spans) > 2 else rival_span]},
            ]
            bad = [dict(it, evidence_ids=[f"{self.artifact}:v2:L99"]) if it["item"] == "결과물 품질" else it for it in items]
            r = await s.call("business", "save_evaluation_draft", {"evaluation_id": eid, "rubric_version": self.rubric_version,
                                                                   "items": bad, "idempotency_key": new_key("e2e")})
            check("없는 구간 근거 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
            r = await s.call("business", "save_evaluation_draft", {"evaluation_id": eid, "rubric_version": self.rubric_version,
                                                                   "items": items, "idempotency_key": new_key("e2e")})
            check("save_evaluation_draft: 메시지 ID·나눈 구간 ID를 근거로 저장", r.get("ok"), str(r)[:300])
        det = internal("GET", f"/internal/evaluations/{eid}")
        check("평가 건 상세에 초안 저장됨", det.get("ok") and det["data"]["draft"] and len(det["data"]["draft"]["items"]) == 5, str(det)[:200])

    def cleanup(self) -> None:
        self.db.execute("DELETE FROM evaluations WHERE work_id=%s", (self.work,))
        self.db.execute("DELETE FROM work_cases WHERE work_id=%s", (self.work,))
        left = self.one("SELECT count(*) AS n FROM llm_exchanges WHERE work_id=%s", (self.work,))["n"]
        check("정리: 시험 업무와 대화 기록 삭제", left == 0 and not self.one("SELECT 1 FROM work_cases WHERE work_id=%s", (self.work,)))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="시험 업무를 지우지 않는다")
    a = ap.parse_args()
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
    sc = Scenario(conn)
    sc.span_rule()
    sc.create_work()
    try:
        sc.live_before_chat()
        if sc.wait_llm():
            sc.conversation()
            sc.header_checks()
            sc.db_checks()
            if len(getattr(sc, "messages", [])) == 12:
                await sc.live()
                sc.save_artifacts()
                await sc.final()
    finally:
        if not a.keep:
            sc.cleanup()
        else:
            print(f"(남김) {sc.work}")
    return report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
