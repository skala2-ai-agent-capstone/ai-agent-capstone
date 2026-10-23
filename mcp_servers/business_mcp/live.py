"""중간 피드백: 최근 완료 질문·응답 5쌍에 대한 코칭 (live_evaluations). 계약 정본: docs/recent_five_feedback.md

백엔드가 요청 행(대기)을 만들어 큐에 발행하고, 피드백 Worker가 Worker 토큰으로
start_live_evaluation → get_messages → save_live_evaluation 순서로 부른다.
- 범위: 요청 시점까지 성공한 최근 질문·응답 최대 5쌍. 마스킹본을 요청 행에 고정하며 0쌍이면 요청하지 않는다.
- 유형·기준: 요청 때 고정한다(work_type, rubric_doc_id·rubric_version). 저장 때 다른 값으로 바꿀 수 없다.
- 권한: Worker 토큰은 회사 범위다. 매 호출마다 live_eval_id로 요청 행을 찾아 토큰의 회사와 같은지 확인한다.
- 중복: 키 없이 상태로 처리한다. 대기 → 생성 중 → 완료|실패. 완료·실패는 다시 바꾸지 않고,
  큐 재전달로 같은 호출이 다시 오면 현재 상태를 돌려준다(start는 문맥, save는 duplicate:true).
응답 data는 Agent 계약 모델(모르는 칸 거부)과 칸이 정확히 같아야 한다.
"""
from __future__ import annotations

import json
import re

from common.runtime import MCPFail, forbidden, new_id, not_found, validation, version_number
from psycopg.types.json import Jsonb

from .pg import Db, iso, iso_row
from .store import Store

PAIRS = 5                        # 최근 완료 질문·응답 쌍 수
RATE_LIMIT_SEC = 300             # 사용자별 요청 간격 (직원 대화와 같은 GPU를 쓰므로)
DONE = ("완료", "실패")           # 확정 상태. 이후 호출로 바꾸지 않는다
WORK_TYPES = ("rfp_analysis", "proposal", "quote", "inquiry_reply")
LIVE_COLS = ("live_eval_id, work_id, requested_by, requested_at, work_type, from_seq, to_seq, rubric_doc_id, rubric_version, "
             "status, items, coaching, model, latency_ms, error")

# 결과 형식 (Agent 계약 GeneratedFeedback·coaching v1)
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
KINDS = {"strength", "improvement", "needs_check"}
MAX_ITEMS, MAX_EVIDENCE, TEXT_MAX, QUOTE_MAX = 3, 4, 2000, 500
PROVENANCE_MAX_BYTES = 20_000
ITEM_KEYS = {"item_id", "criterion_id", "kind", "diagnosis", "evidence"}
EVIDENCE_KEYS = {"message_id", "quote"}
COACHING_KEYS = {"schema_version", "provenance", "suggestions"}
SUGGESTION_KEYS = {"item_id", "target_message_id", "next_action", "suggested_prompt"}


class LiveStore:
    def __init__(self, db: Db, store: Store):
        self.db = db
        self.store = store

    # ── 백엔드 역할(내부 API) ──
    def create_request(self, body: dict) -> tuple[dict, bool]:
        """요청 행을 만든다. 범위·유형·기준을 여기서 고정한다. 같은 live_eval_id면 기존 행을 돌려준다.
        body: {work_id, requested_by, live_eval_id?, work_type?}. work_type이 없으면 업무의 분류 결과를 쓴다."""
        wid, user, work_type = body.get("work_id"), body.get("requested_by"), body.get("work_type")
        if not (wid and user):
            raise validation("work_id와 requested_by가 필요합니다")
        if work_type is not None and work_type not in WORK_TYPES:
            raise validation(f"work_type은 {', '.join(WORK_TYPES)} 중 하나입니다")
        lid = body.get("live_eval_id") or new_id("LIVE")
        with self.db.tx() as c:
            requested_at = c.execute("SELECT transaction_timestamp() AS at").fetchone()["at"]
            old = c.execute(f"SELECT {LIVE_COLS} FROM live_evaluations WHERE live_eval_id=%s", (lid,)).fetchone()
            if old:
                return iso_row(old), False
            work = c.execute("SELECT company_id, employee_id, work_type FROM work_cases WHERE work_id=%s", (wid,)).fetchone()
            if not work:
                raise not_found(f"업무 {wid}")
            if work["employee_id"] != user:
                raise forbidden("업무 담당자만 중간 피드백을 요청할 수 있습니다")
            # 같은 사용자의 동시 요청이 함께 통과하지 않도록 사용자 행을 잠근다
            c.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (user,))
            recent = c.execute(
                "SELECT requested_at FROM live_evaluations WHERE requested_by=%s AND requested_at > now() - make_interval(secs => %s) "
                "ORDER BY requested_at DESC LIMIT 1", (user, RATE_LIMIT_SEC)).fetchone()
            if recent:
                raise MCPFail("RATE_LIMITED", f"중간 피드백은 5분에 한 번만 요청할 수 있습니다 (마지막 요청 {iso(recent['requested_at'])})")
            messages = self._recent_messages(c, wid, requested_at)
            if not messages:
                raise validation("완료된 질문·응답이 아직 없어 중간 피드백을 요청할 수 없습니다")
            work_type = work_type or work["work_type"]
            doc_id, version = self._current_rubric(c, work["company_id"], work_type) if work_type else (None, None)
            row = c.execute(
                "INSERT INTO live_evaluations (live_eval_id, work_id, requested_by, requested_at, work_type, from_seq, to_seq, "
                "rubric_doc_id, rubric_version, conversation_snapshot) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {LIVE_COLS}",
                (lid, wid, user, requested_at, work_type, messages[0]["seq"], messages[-1]["seq"], doc_id, version,
                 Jsonb({"masking_version": self.store.masking_version, "messages": messages}))).fetchone()
        return iso_row(row), True

    @staticmethod
    def _recent_messages(c, work_id: str, requested_at) -> list[dict]:
        """요청 전에 성공한 최근 5회. 같은 SQL 스냅샷에서 짝·본문을 읽어 범위 재계산을 막는다.

        llm_exchanges.request에는 이전 대화도 있으므로 messages/conversation_pairs로 현재 질문만
        추출한다. 응답 행의 마지막 질문과도 대조해 서로 다른 호출의 질문·답변을 잘못 묶지 않는다.
        """
        rows = c.execute("""
            WITH selected AS (
                SELECT p.*, q.content_masked AS question, a.content_masked AS answer,
                       q.created_at AS question_at, a.created_at AS answer_at
                FROM conversation_pairs(%s) p
                JOIN messages q ON q.message_id = p.q_message_id
                JOIN messages a ON a.message_id = p.a_message_id
                JOIN llm_exchanges e ON e.id = p.exchange_id
                WHERE e.stage = 'workspace_chat' AND e.status_code BETWEEN 200 AND 299
                  AND e.error IS NULL AND e.created_at <= %s
                  AND q.created_at <= %s AND a.created_at <= %s
                  AND btrim(q.content) <> '' AND btrim(a.content) <> ''
                  AND q.content = (
                      SELECT CASE WHEN m->>'role' = 'user' THEN m->>'content' END
                      FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.request->'messages') = 'array'
                                                    THEN e.request->'messages' ELSE '[]'::jsonb END)
                           WITH ORDINALITY AS t(m, n)
                      WHERE m->>'role' IN ('user', 'assistant')
                      ORDER BY n DESC LIMIT 1
                  )
                ORDER BY e.created_at DESC, e.id DESC LIMIT %s
            )
            SELECT q_message_id AS message_id, exchange_id, q_seq AS seq, 'user' AS role,
                   question AS content, question_at AS created_at FROM selected
            UNION ALL
            SELECT a_message_id, exchange_id, a_seq, 'assistant', answer, answer_at FROM selected
            ORDER BY seq
        """, (work_id, requested_at, requested_at, requested_at, PAIRS)).fetchall()
        return [iso_row(row) for row in rows]

    def get(self, live_eval_id: str) -> dict:
        row = self.db.one(f"SELECT {LIVE_COLS} FROM live_evaluations WHERE live_eval_id=%s", (live_eval_id,))
        if not row:
            raise not_found(f"중간 피드백 {live_eval_id}")
        return iso_row(row)

    @staticmethod
    def _current_rubric(c, company_id: str, work_type: str) -> tuple[str | None, str | None]:
        rows = c.execute("SELECT doc_id, version FROM documents WHERE collection='rubrics' AND status='현행' AND company_id=%s "
                         "AND work_type=%s", (company_id, work_type)).fetchall()
        best = max(rows, key=lambda r: version_number(r["version"]), default=None)
        return (best["doc_id"], best["version"]) if best else (None, None)

    # ── Worker 도구(live 토큰) ──
    def _request(self, claims: dict, live_eval_id: str, c=None) -> dict:
        """요청 행과 회사. c(트랜잭션)가 있으면 행을 잠근다. 토큰의 회사와 다르면 FORBIDDEN."""
        sql = (f"SELECT l.{LIVE_COLS.replace(', ', ', l.')}, l.conversation_snapshot, w.company_id "
               "FROM live_evaluations l JOIN work_cases w USING (work_id) "
               "WHERE l.live_eval_id=%s")
        row = c.execute(sql + " FOR UPDATE OF l", (live_eval_id,)).fetchone() if c is not None else self.db.one(sql, (live_eval_id,))
        if not row:
            raise not_found(f"중간 피드백 {live_eval_id}")
        if row["company_id"] != claims["company_id"]:
            raise forbidden(f"{live_eval_id}는 이 Worker 토큰의 회사 요청이 아닙니다")
        return row

    def start(self, claims: dict, live_eval_id: str) -> dict:
        """대기면 생성 중으로 바꾸고 고정된 문맥을 돌려준다. 생성 중(재전달)은 같은 문맥, 완료·실패는 바꾸지 않고 그대로."""
        with self.db.tx() as c:
            row = self._request(claims, live_eval_id, c)
            if row["status"] == "대기":
                c.execute("UPDATE live_evaluations SET status='생성 중' WHERE live_eval_id=%s", (live_eval_id,))
                row["status"] = "생성 중"
        rubric = None
        if row["rubric_doc_id"]:
            items = self.db.q("SELECT item_id, item, criterion FROM rubric_items WHERE doc_id=%s AND version=%s ORDER BY seq",
                              (row["rubric_doc_id"], row["rubric_version"]))
            if not items:   # 고정한 기준을 조용히 빼면 안 된다
                raise not_found(f"고정한 평가 기준 {row['rubric_doc_id']} {row['rubric_version']}")
            rubric = {"doc_id": row["rubric_doc_id"], "version": row["rubric_version"], "items": items}
        return {"live_eval_id": live_eval_id, "company_id": row["company_id"], "work_id": row["work_id"],
                "requested_by": row["requested_by"], "requested_at": iso(row["requested_at"]), "from_seq": row["from_seq"],
                "to_seq": row["to_seq"], "status": row["status"], "work_type": row["work_type"],
                "rubric_doc_id": row["rubric_doc_id"], "rubric_version": row["rubric_version"], "rubric": rubric}

    @staticmethod
    def _snapshot(row: dict) -> dict:
        snapshot = row["conversation_snapshot"]
        if snapshot is None:
            # 이전 요청의 범위를 현재 뷰에서 재구성하면 당시와 다른 내용을 평가할 수 있다.
            raise validation("대화 스냅샷이 없는 이전 요청입니다. 중간 피드백을 새로 요청하세요")
        return snapshot

    def get_messages(self, claims: dict, live_eval_id: str, work_id: str, from_seq: int, to_seq: int) -> dict:
        row = self._request(claims, live_eval_id)
        if (work_id, from_seq, to_seq) != (row["work_id"], row["from_seq"], row["to_seq"]):
            raise validation(f"요청에 고정된 범위와 다릅니다 (고정 {row['work_id']} {row['from_seq']}~{row['to_seq']})")
        if row["status"] != "생성 중":
            raise validation(f"상태가 '{row['status']}'입니다. start_live_evaluation 뒤 생성 중일 때만 대화를 읽습니다")
        snapshot = self._snapshot(row)
        messages = snapshot["messages"]
        complete = (bool(messages) and len(messages) <= 2 * PAIRS
                    and messages[0]["seq"] == from_seq and messages[-1]["seq"] == to_seq)
        return {"live_eval_id": live_eval_id, "work_id": work_id, "from_seq": from_seq, "to_seq": to_seq,
                "masking_applied": True, "masking_version": snapshot["masking_version"], "complete": complete,
                "messages": messages}

    def save(self, claims: dict, live_eval_id: str, status: str, items: list | None, coaching: dict | None,
             model: str | None, latency_ms: int | None, error: str | None) -> dict:
        """결과를 저장한다. 이미 완료·실패면 덮어쓰지 않고 현재 상태를 duplicate:true로 돌려준다."""
        if model is not None and (not isinstance(model, str) or len(model) > 200):
            raise validation("model은 200자 이하 문자열입니다")
        if latency_ms is not None and (not isinstance(latency_ms, int) or isinstance(latency_ms, bool) or latency_ms < 0):
            raise validation("latency_ms는 0 이상의 정수입니다")
        with self.db.tx() as c:
            row = self._request(claims, live_eval_id, c)
            if row["status"] in DONE:
                return {"live_eval_id": live_eval_id, "status": row["status"], "duplicate": True}
            if row["status"] != "생성 중":
                raise validation("start_live_evaluation을 먼저 불러야 합니다 (현재 대기)")
            if status == "완료":
                if error is not None:
                    raise validation("완료 결과에는 error를 넣지 않습니다")
                messages = {m["message_id"]: m for m in self._snapshot(row)["messages"]}
                criteria = {r["item_id"] for r in c.execute("SELECT item_id FROM rubric_items WHERE doc_id=%s AND version=%s",
                                                            (row["rubric_doc_id"], row["rubric_version"]))}
                clean_items = check_items(items, messages, criteria, self.store.masker)
                clean_coaching = check_coaching(coaching, clean_items, messages, self.store.masker)
                c.execute("UPDATE live_evaluations SET status='완료', items=%s, coaching=%s, model=%s, latency_ms=%s, error=NULL "
                          "WHERE live_eval_id=%s", (Jsonb(clean_items), Jsonb(clean_coaching), model, latency_ms, live_eval_id))
            elif status == "실패":
                if not isinstance(error, str) or not ERROR_CODE_RE.match(error):
                    raise validation("실패에는 error 코드(대문자·숫자·밑줄, 예 MODEL_TIMEOUT)가 필요합니다")
                if items or coaching:
                    raise validation("실패에는 items·coaching을 넣지 않습니다")
                c.execute("UPDATE live_evaluations SET status='실패', model=%s, latency_ms=%s, error=%s WHERE live_eval_id=%s",
                          (model, latency_ms, error, live_eval_id))
            else:
                raise validation("status는 완료 또는 실패입니다")
        return {"live_eval_id": live_eval_id, "status": status, "duplicate": False}


# ── 결과 검증 (Agent 계약과 같은 규칙을 서버에서도 강제) ──
def _text(v, limit: int) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) and len(v) <= limit


def check_items(items, messages: dict[str, dict], criteria: set[str], masker) -> list[dict]:
    """진단 1~3개 {item_id, criterion_id, kind, diagnosis, evidence[{message_id, quote}]}.
    근거는 범위 안 메시지의 실제 문장(연속 인용)이어야 하고, criterion_id는 고정된 기준의 항목이어야 한다."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise validation(f"items는 1~{MAX_ITEMS}개입니다")
    errors, seen = [], set()
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict) or set(it) - ITEM_KEYS:
            errors.append(f"#{n}: 칸은 {sorted(ITEM_KEYS)}만 씁니다")
            continue
        iid = it.get("item_id")
        if not isinstance(iid, str) or not ID_RE.match(iid) or iid in seen:
            errors.append(f"#{n}: item_id가 없거나 형식이 틀리거나 중복입니다")
        else:
            seen.add(iid)
        cid = it.get("criterion_id")
        if cid is not None and (not isinstance(cid, str) or cid not in criteria):
            errors.append(f"#{n}: criterion_id {cid}는 고정된 기준의 항목이 아닙니다")
        if it.get("kind") not in KINDS:
            errors.append(f"#{n}: kind는 {sorted(KINDS)} 중 하나")
        if not _text(it.get("diagnosis"), TEXT_MAX):
            errors.append(f"#{n}: diagnosis는 1~{TEXT_MAX}자")
        elif masker.has_sensitive(it["diagnosis"]):
            errors.append(f"#{n}: diagnosis에 민감정보 패턴")
        ev = it.get("evidence")
        if not isinstance(ev, list) or not 1 <= len(ev) <= MAX_EVIDENCE:
            errors.append(f"#{n}: evidence는 1~{MAX_EVIDENCE}개")
            continue
        for e in ev:
            if not isinstance(e, dict) or set(e) != EVIDENCE_KEYS:
                errors.append(f"#{n}: evidence 칸은 message_id·quote")
                continue
            m = messages.get(e["message_id"]) if isinstance(e["message_id"], str) else None
            if m is None:
                errors.append(f"#{n}: 범위 밖 메시지 {e['message_id']}")
            elif not _text(e["quote"], QUOTE_MAX) or e["quote"] not in m["content"]:
                errors.append(f"#{n}: quote가 {e['message_id']}의 실제 문장이 아닙니다")
    if errors:
        raise validation("; ".join(errors))
    return [{"item_id": it["item_id"], "criterion_id": it.get("criterion_id"), "kind": it["kind"], "diagnosis": it["diagnosis"],
             "evidence": [{"message_id": e["message_id"], "quote": e["quote"]} for e in it["evidence"]]} for it in items]


def check_coaching(coaching, items: list[dict], messages: dict[str, dict], masker) -> dict:
    """{schema_version:'1', provenance{…}, suggestions[{item_id, target_message_id, next_action, suggested_prompt}]}.
    추천은 직원 질문을 가리키고, 항목마다 최대 1개, 개선(improvement) 항목에는 반드시 있어야 한다."""
    if not isinstance(coaching, dict) or set(coaching) != COACHING_KEYS:
        raise validation(f"coaching 칸은 {sorted(COACHING_KEYS)}입니다")
    if coaching["schema_version"] != "1":
        raise validation("coaching.schema_version은 '1'입니다")
    prov = coaching["provenance"]
    if not isinstance(prov, dict) or len(json.dumps(prov, ensure_ascii=False).encode()) > PROVENANCE_MAX_BYTES:
        raise validation(f"provenance는 {PROVENANCE_MAX_BYTES}바이트 이하 객체입니다")
    sugg = coaching["suggestions"]
    if not isinstance(sugg, list) or not 1 <= len(sugg) <= MAX_ITEMS:
        raise validation(f"suggestions는 1~{MAX_ITEMS}개입니다")
    kinds = {it["item_id"]: it["kind"] for it in items}
    errors, coached = [], set()
    for n, s in enumerate(sugg, 1):
        if not isinstance(s, dict) or set(s) != SUGGESTION_KEYS:
            errors.append(f"#{n}: 칸은 {sorted(SUGGESTION_KEYS)}")
            continue
        if not isinstance(s["item_id"], str) or s["item_id"] not in kinds or s["item_id"] in coached:
            errors.append(f"#{n}: item_id {s['item_id']}는 진단 항목이 아니거나 이미 추천이 있습니다")
        else:
            coached.add(s["item_id"])
        target = messages.get(s["target_message_id"]) if isinstance(s["target_message_id"], str) else None
        if target is None or target["role"] != "user":
            errors.append(f"#{n}: target_message_id는 범위 안 직원 질문이어야 합니다")
        for f in ("next_action", "suggested_prompt"):
            if not _text(s[f], TEXT_MAX):
                errors.append(f"#{n}: {f}는 1~{TEXT_MAX}자")
            elif masker.has_sensitive(s[f]):
                errors.append(f"#{n}: {f}에 민감정보 패턴")
    missing = [i for i, k in kinds.items() if k == "improvement" and i not in coached]
    if missing:
        errors.append(f"개선 항목 {missing}에 추천이 없습니다")
    if errors:
        raise validation("; ".join(errors))
    return {"schema_version": "1", "provenance": prov,
            "suggestions": [{k: s[k] for k in ("item_id", "target_message_id", "next_action", "suggested_prompt")} for s in sugg]}
