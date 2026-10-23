"""앱(백엔드 업무 공간)이 만든 업무 모양 검증: 고객 미지정 업무와 원본 파일 첨부. 실행 중인 두 MCP와 DB가 필요하다.

백엔드 WorkspaceService와 같은 칸으로 업무·첨부를 넣고(003_frontend_upload.sql), MCP 조회와 정책 검사가 깨지지 않는지 본다.
- 고객 없이 만든 업무: get_work_case의 customer는 null, 다른 고객사 언급(V-OTHER-CUSTOMER)은 검사하지 않고 note에 알린다
- 같은 산출물이라도 고객이 있는 업무는 그대로 V-OTHER-CUSTOMER로 잡는다(대조군)
- 첨부의 원본 칸(file_name·content_type·size_bytes·file_data)은 MCP 응답에 나가지 않는다
만든 업무는 끝나면 지운다. 대화 없이 산출물만 쓰므로 llm-proxy는 필요 없다.
    python tests/verify_app_work.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Session, check, internal, report  # noqa: E402

RFP_TEXT = "제안요청서\n1. 사업명: 계약서 검토 자동화\n2. 제출 기한: 2026-11-30"
FILE_KEYS = {"file_name", "content_type", "size_bytes", "file_data"}


class AppWork:
    """백엔드 「새 업무 작성」 → 고객 자료 업로드 → 산출물 저장 → 완료와 같은 행을 넣는다."""

    def __init__(self, db, owner: dict, customer_id: str | None, rival_name: str):
        self.db, self.owner, self.customer_id = db, owner, customer_id
        tag = uuid.uuid4().hex[:6].upper()
        self.work, self.artifact, self.attachment = f"W-APP-{tag}", f"ART-APP-{tag}", f"ATT-APP-{tag}"
        self.text = f"# 계약서 검토 자동화 제안\n\n1. 구축 일정: 착수 후 12주.\n2. 참고 사례: {rival_name} 구축 경험을 바탕으로 합니다."

    def create(self) -> str:
        o = self.owner
        self.db.execute(
            "INSERT INTO work_cases (work_id, company_id, team_id, employee_id, work_type, customer_id, title, status, created_at, "
            "registration, registration_masked) VALUES (%s,%s,%s,%s,'proposal',%s,'계약서 검토 자동화 제안','in_progress',now(),NULL,NULL)",
            (self.work, o["company_id"], o["team_id"], o["user_id"], self.customer_id))
        self.db.execute(
            "INSERT INTO source_documents (attachment_id, work_id, doc_id, doc_type, version, grade, uploaded_by, uploaded_at, content, "
            "content_masked, file_name, content_type, size_bytes, file_data) "
            "VALUES (%s,%s,'RFP-APP','rfp','v1','대외비',%s,now(),%s,mask_text(%s),'제안요청서.pdf','application/pdf',%s,%s)",
            (self.attachment, self.work, o["user_id"], RFP_TEXT, RFP_TEXT, 4, b"%PDF"))
        self.db.execute("INSERT INTO artifact_versions (artifact_id, version, work_id, author, saved_at, content, content_masked) "
                        "VALUES (%s,'v1',%s,'employee',now(),%s,mask_text(%s))", (self.artifact, self.work, self.text, self.text))
        rubric = self.db.execute("SELECT version FROM documents WHERE collection='rubrics' AND work_type='proposal' AND status='현행' "
                                 "AND company_id=%s ORDER BY length(version) DESC, version DESC LIMIT 1",
                                 (o["company_id"],)).fetchone()["version"]
        key = f"{self.work}:v1:{rubric}"
        self.db.execute("UPDATE work_cases SET status='completed', completed_at=now() WHERE work_id=%s", (self.work,))
        self.db.execute("INSERT INTO work_events (event_id, work_id, event_type, artifact_version, rubric_version, idempotency_key, "
                        "occurred_at) VALUES (%s,%s,'work.completed','v1',%s,%s,now())", (f"EVT-{uuid.uuid4().hex[:8]}", self.work, rubric, key))
        ev = internal("POST", "/internal/evaluations",
                      {"work_id": self.work, "artifact_version": "v1", "rubric_version": rubric, "idempotency_key": key})
        return internal("POST", "/internal/runs", {"evaluation_id": ev["data"]["evaluation_id"]})["data"]["run_token"]

    def cleanup(self) -> None:
        self.db.execute("DELETE FROM evaluations WHERE work_id=%s", (self.work,))
        self.db.execute("DELETE FROM work_cases WHERE work_id=%s", (self.work,))


async def main() -> int:
    db = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
    owner = db.execute("SELECT user_id, company_id, team_id FROM users WHERE role='member' AND team_id IS NOT NULL "
                       "ORDER BY user_id LIMIT 1").fetchone()
    custs = db.execute("SELECT customer_id, name, approved_reference FROM customers WHERE company_id=%s ORDER BY customer_id",
                       (owner["company_id"],)).fetchall()
    rival = next(c for c in custs if not c["approved_reference"])
    own = next(c for c in custs if c["customer_id"] != rival["customer_id"])
    rival_name = re.sub(r"\(가상\)$", "", rival["name"]).strip()

    no_customer = AppWork(db, owner, None, rival_name)
    with_customer = AppWork(db, owner, own["customer_id"], rival_name)
    try:
        tok = no_customer.create()
        check("고객 없이 만든 업무·원본 파일 첨부 저장(백엔드와 같은 칸)", tok)
        async with Session(tok) as s:
            wc = await s.call("business", "get_work_case", {"work_id": no_customer.work})
            d = wc.get("data") or {}
            check("get_work_case: customer는 null", wc["ok"] and d.get("customer") == {"customer_id": None, "name": None}, str(wc)[:200])
            docs = d.get("source_documents") or []
            check("get_work_case: 첨부 목록에 원본 파일 칸 없음", docs and not (FILE_KEYS & set(docs[0])), str(docs)[:200])
            sd = await s.call("business", "get_source_document", {"attachment_id": no_customer.attachment})
            sdd = sd.get("data") or {}
            # 응답의 content_type은 MCP의 '데이터 표시'(값 "data")로, 첨부의 MIME 칸(content_type)과 이름만 같다
            check("get_source_document: 꺼낸 텍스트만, 원본 파일 칸 없음",
                  sd["ok"] and "계약서 검토 자동화" in str(sdd) and not ((FILE_KEYS - {"content_type"}) & set(sdd))
                  and sdd.get("content_type") == "data", str(sd)[:200])
            pol = await s.call("business", "get_policy_check_result", {"work_id": no_customer.work, "version": "v1"})
            pd = pol.get("data") or {}
            rules = {v["rule_id"] for v in pd.get("violations", [])}
            check("정책 검사: 고객 미지정 업무는 V-OTHER-CUSTOMER 없음", pol["ok"] and "V-OTHER-CUSTOMER" not in rules, str(pol)[:300])
            check("정책 검사: note에 검사하지 않은 규칙을 알림", "V-OTHER-CUSTOMER" in pd.get("scope", {}).get("note", ""), str(pd.get("scope")))
            check("정책 검사: scope 칸은 그대로(Agent 엄격 모델)", set(pd.get("scope", {})) == {"user_message_ids", "artifact_ref", "note"})

        tok = with_customer.create()
        async with Session(tok) as s:
            pol = await s.call("business", "get_policy_check_result", {"work_id": with_customer.work, "version": "v1"})
            pd = pol.get("data") or {}
            hits = [v for v in pd.get("violations", []) if v["rule_id"] == "V-OTHER-CUSTOMER"]
            check("대조군: 고객이 있으면 같은 산출물에서 V-OTHER-CUSTOMER", pol["ok"] and hits
                  and hits[0].get("matched_customer_id") == rival["customer_id"], str(pol)[:300])
            check("대조군: note에 건너뜀 문구 없음", "V-OTHER-CUSTOMER" not in pd.get("scope", {}).get("note", ""))
    finally:
        no_customer.cleanup()
        with_customer.cleanup()
    left = db.execute("SELECT count(*) AS n FROM work_cases WHERE work_id IN (%s,%s)", (no_customer.work, with_customer.work)).fetchone()["n"]
    check("정리: 시험 업무 삭제", left == 0)
    db.close()
    return report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
