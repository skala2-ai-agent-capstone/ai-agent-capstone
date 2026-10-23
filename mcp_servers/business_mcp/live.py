"""중간 점검·실시간 피드백 (live_evaluations).

백엔드가 요청 행(대기)을 만들어 큐에 발행하고, Agent가 live 실행 토큰으로 결과를 저장한다.
- 범위: 요청 시점의 최근 메시지 5개(messages.seq). Agent는 이 범위의 대화만 읽고 근거로 인용할 수 있다.
- 상태: 대기 → 생성 중 → 완료 | 실패 (폴링용). 완료·실패 뒤에는 바꾸지 않는다.
- 항목 형식은 최종 평가 초안·코칭과 같다. 산출물이 없으므로 결과물 품질은 미관측만 허용한다.
- 같은 사용자의 요청은 5분에 1회(직원 대화와 같은 GPU를 쓰므로).
모든 쓰기는 idempotency_key가 필수이고 같은 키는 기존 결과를 돌려준다.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from common.runtime import LIVE, MCPFail, forbidden, new_id, not_found, validation

from .checks import check_coaching, check_items, check_text
from .idempotency import need_key, remember, replay
from .pg import Db, iso, iso_row
from .store import Store

WINDOW = 5                       # 최근 메시지 수
RATE_LIMIT_SEC = 300             # 사용자별 요청 간격
OPEN = ("대기", "생성 중")        # 완료·실패 뒤에는 바꾸지 않는다
LIVE_COLS = ("live_eval_id, work_id, requested_by, requested_at, from_seq, to_seq, rubric_doc_id, rubric_version, status, "
             "items, coaching, model, latency_ms, error")
OUTPUT_ITEM = "결과물 품질"


def _output_unobserved(name: str, item: dict) -> str | None:
    """중간 점검에는 산출물이 없다."""
    if name == OUTPUT_ITEM and item.get("status") != "미관측":
        return f"중간 점검에는 산출물이 없어 {OUTPUT_ITEM}은 미관측만 허용"
    return None


class LiveStore:
    def __init__(self, db: Db, store: Store):
        self.db = db
        self.store = store

    # ── 백엔드 역할(내부 API) ──
    def create_request(self, body: dict) -> tuple[dict, bool]:
        """요청 행을 만든다. 같은 live_eval_id면 기존 행을 돌려준다."""
        wid, user = body.get("work_id"), body.get("requested_by")
        if not (wid and user):
            raise validation("work_id와 requested_by가 필요합니다")
        lid = body.get("live_eval_id") or new_id("LIVE")
        with self.db.tx() as c:
            old = c.execute(f"SELECT {LIVE_COLS} FROM live_evaluations WHERE live_eval_id=%s", (lid,)).fetchone()
            if old:
                return iso_row(old), False
            work = c.execute("SELECT work_id, company_id, employee_id FROM work_cases WHERE work_id=%s", (wid,)).fetchone()
            if not work:
                raise not_found(f"업무 {wid}")
            if work["employee_id"] != user:
                raise forbidden("업무 담당자만 중간 점검을 요청할 수 있습니다")
            # 같은 사용자의 동시 요청이 함께 통과하지 않도록 사용자 행을 잠근다
            c.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (user,))
            recent = c.execute(
                "SELECT requested_at FROM live_evaluations WHERE requested_by=%s AND requested_at > now() - make_interval(secs => %s) "
                "ORDER BY requested_at DESC LIMIT 1", (user, RATE_LIMIT_SEC)).fetchone()
            if recent:
                raise MCPFail("RATE_LIMITED", f"중간 점검은 5분에 한 번만 요청할 수 있습니다 (마지막 요청 {iso(recent['requested_at'])})")
            last = c.execute("SELECT max(seq) AS n FROM messages WHERE work_id=%s", (wid,)).fetchone()["n"]
            if not last:
                raise validation("아직 대화가 없어 중간 점검을 할 수 없습니다")
            row = c.execute(
                f"INSERT INTO live_evaluations (live_eval_id, work_id, requested_by, from_seq, to_seq) VALUES (%s,%s,%s,%s,%s) "
                f"RETURNING {LIVE_COLS}", (lid, wid, user, max(1, last - WINDOW + 1), last)).fetchone()
        return iso_row(row), True

    def get(self, live_eval_id: str) -> dict:
        row = self.db.one(f"SELECT {LIVE_COLS} FROM live_evaluations WHERE live_eval_id=%s", (live_eval_id,))
        if not row:
            raise not_found(f"중간 점검 {live_eval_id}")
        return iso_row(row)

    def claims_for(self, live_eval_id: str, run_id: str) -> dict:
        live = self.get(live_eval_id)
        if live["status"] not in OPEN:
            raise validation(f"이미 '{live['status']}'인 중간 점검입니다")
        wc = self.store.raw[live["work_id"]]["work_case"]
        return {
            "kind": LIVE,
            "run_id": run_id,
            "company_id": wc["company_id"],
            "team_id": wc.get("team_id"),
            "employee_id": wc["employee_id"],
            "live_eval_id": live_eval_id,
            "work_id": live["work_id"],
            "work_type": wc.get("work_type"),
            "snapshot": {"conversation_range": {"from_seq": live["from_seq"], "to_seq": live["to_seq"]}},
        }

    # ── Agent 도구(live 토큰) ──
    def _open(self, claims: dict, live_eval_id: str, c, action: str) -> dict:
        """토큰의 중간 점검 행을 잠그고, 아직 대기·생성 중인지 확인한다. action: 거절할 때 문구."""
        if live_eval_id != claims.get("live_eval_id"):
            raise forbidden(f"{live_eval_id}는 이 실행의 중간 점검이 아닙니다")
        row = c.execute(f"SELECT {LIVE_COLS} FROM live_evaluations WHERE live_eval_id=%s FOR UPDATE", (live_eval_id,)).fetchone()
        if not row:
            raise not_found(f"중간 점검 {live_eval_id}")
        if row["status"] not in OPEN:
            raise validation(f"상태가 '{row['status']}'라 {action}")
        return iso_row(row)

    def _pool(self, claims: dict) -> set[str]:
        rng = claims["snapshot"]["conversation_range"]
        return {m["message_id"] for m in self.store.raw[claims["work_id"]]["messages"]
                if rng["from_seq"] <= m["seq"] <= rng["to_seq"]}

    def start(self, claims: dict, live_eval_id: str, idempotency_key: str):
        need_key(idempotency_key)
        with self.db.tx() as c:
            if (dup := replay(c, "start_live_evaluation", idempotency_key)) is not None:
                return dup
            self._open(claims, live_eval_id, c, "시작할 수 없습니다")
            c.execute("UPDATE live_evaluations SET status='생성 중' WHERE live_eval_id=%s", (live_eval_id,))
            result = {"live_eval_id": live_eval_id, "status": "생성 중"}
            remember(c, "start_live_evaluation", idempotency_key, result)
            return result

    def save(self, claims: dict, live_eval_id: str, work_type: str, rubric_version: str, items: list, coaching: list,
             idempotency_key: str, model: str | None, latency_ms: int | None):
        need_key(idempotency_key)
        pool = self._pool(claims)
        rubric = self.store.rubric(work_type, rubric_version)
        with self.db.tx() as c:
            if (dup := replay(c, "save_live_evaluation", idempotency_key)) is not None:
                return dup
            self._open(claims, live_eval_id, c, "결과를 저장할 수 없습니다")
            clean_items = check_items(items, rubric["items"], pool, self.store.masker,
                                      outside="점검 범위(최근 메시지) 밖의 근거 ID", rule=_output_unobserved)
            clean_coaching = check_coaching(coaching, {i["item_id"] for i in clean_items}, pool, self.store.masker,
                                            not_saved="이 점검의 항목이 아님", outside="점검 범위 밖")
            if latency_ms is not None and (not isinstance(latency_ms, int) or isinstance(latency_ms, bool) or latency_ms < 0):
                raise validation("latency_ms는 0 이상의 정수입니다")
            c.execute(
                "UPDATE live_evaluations SET status='완료', rubric_doc_id=%s, rubric_version=%s, items=%s, coaching=%s, model=%s, "
                "latency_ms=%s, error=NULL WHERE live_eval_id=%s",
                (rubric["doc_id"], rubric_version, Jsonb(clean_items), Jsonb(clean_coaching), model, latency_ms, live_eval_id))
            result = {"live_eval_id": live_eval_id, "status": "완료", "rubric_doc_id": rubric["doc_id"], "rubric_version": rubric_version,
                      "items_saved": len(clean_items), "coaching_saved": len(clean_coaching)}
            remember(c, "save_live_evaluation", idempotency_key, result)
            return result

    def fail(self, claims: dict, live_eval_id: str, error: str, idempotency_key: str):
        need_key(idempotency_key)
        if not error or not isinstance(error, str):
            raise validation("error가 필요합니다")
        check_text(self.store.masker, error, "error")
        with self.db.tx() as c:
            if (dup := replay(c, "fail_live_evaluation", idempotency_key)) is not None:
                return dup
            self._open(claims, live_eval_id, c, "실패로 바꿀 수 없습니다")
            c.execute("UPDATE live_evaluations SET status='실패', error=%s WHERE live_eval_id=%s", (error[:500], live_eval_id))
            result = {"live_eval_id": live_eval_id, "status": "실패"}
            remember(c, "fail_live_evaluation", idempotency_key, result)
            return result
