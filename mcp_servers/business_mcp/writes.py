"""저장 도구 4개와 평가 건 상태 (명세 3.2). 저장소는 PostgreSQL.

평가 초안·코칭·검토 요청·진행 상태만 쓴다. 확정·승인·본인 확인·집계 반영은 노출하지 않는다.
멱등 처리는 idempotency.py, 저장 내용 검증은 checks.py(중간 점검과 공용)에 있다.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from common.runtime import eval_id_for, forbidden, new_id, not_found, validation, version_conflict

from .checks import check_coaching, check_items, check_text
from .idempotency import need_key, remember, replay
from .pg import Db, iso, iso_row
from .store import Store

PROGRESS = ["수집 중", "평가 중", "근거 확인 중", "코칭 생성 중"]
SERVER_ONLY = ["확인 대기", "검토 대기", "확정", "처리 실패", "자료 보류"]
WRITABLE = ["대기"] + PROGRESS
EVAL_COLS = "evaluation_id, work_id, company_id, artifact_id, artifact_version, rubric_version, status, source_key, created_at, updated_at"


class WriteStore:
    def __init__(self, db: Db, store: Store):
        self.db = db
        self.store = store

    # ── 평가 건 (백엔드 역할의 테스트용 재현) ──
    def create_evaluation(self, event: dict) -> tuple[str, bool]:
        wid = event.get("work_id")
        ver, rver = event.get("artifact_version"), event.get("rubric_version")
        if not (wid and ver and rver):
            raise validation("work_id, artifact_version, rubric_version이 필요합니다")
        if wid not in self.store.raw:
            raise not_found(f"업무 {wid}")
        key = event.get("idempotency_key") or f"{wid}:{ver}:{rver}"
        av = self.store.version_rec(wid, ver)
        company = self.store.raw[wid]["work_case"]["company_id"]
        with self.db.tx() as c:
            row = c.execute("SELECT evaluation_id FROM evaluations WHERE source_key=%s", (key,)).fetchone()
            if row:
                return row["evaluation_id"], False
            eid = eval_id_for(wid, ver)
            if c.execute("SELECT 1 FROM evaluations WHERE evaluation_id=%s", (eid,)).fetchone():
                eid = f"{eid}-{rver}"
            c.execute(
                "INSERT INTO evaluations (evaluation_id, work_id, company_id, artifact_id, artifact_version, rubric_version, source_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)", (eid, wid, company, av["artifact_id"], ver, rver, key))
        return eid, True

    def evaluation(self, evaluation_id: str) -> dict:
        row = self.db.one(f"SELECT {EVAL_COLS} FROM evaluations WHERE evaluation_id=%s", (evaluation_id,))
        if not row:
            raise not_found(f"평가 건 {evaluation_id}")
        return iso_row(row)

    def list_evaluations(self, work_id: str | None = None) -> list[dict]:
        if work_id:
            rows = self.db.q(f"SELECT {EVAL_COLS} FROM evaluations WHERE work_id=%s ORDER BY evaluation_id", (work_id,))
        else:
            rows = self.db.q(f"SELECT {EVAL_COLS} FROM evaluations ORDER BY evaluation_id")
        return [iso_row(r) for r in rows]

    def set_status(self, evaluation_id: str, status: str) -> dict:
        if status not in ["대기"] + PROGRESS + SERVER_ONLY:
            raise validation(f"알 수 없는 상태 {status}")
        self.evaluation(evaluation_id)
        self.db.q("UPDATE evaluations SET status=%s, updated_at=now() WHERE evaluation_id=%s RETURNING 1", (status, evaluation_id))
        return self.evaluation(evaluation_id)

    def detail(self, evaluation_id: str) -> dict:
        ev = self.evaluation(evaluation_id)

        def last(table):
            r = self.db.one(f"SELECT revision, items, saved_at FROM {table} WHERE evaluation_id=%s ORDER BY revision DESC LIMIT 1",
                            (evaluation_id,))
            return {"revision": r["revision"], "items": r["items"], "saved_at": iso(r["saved_at"])} if r else None

        ev["draft"] = last("evaluation_drafts")
        ev["coaching"] = last("evaluation_coaching")
        ev["review_requests"] = [
            {**r, "created_at": iso(r["created_at"])}
            for r in self.db.q("SELECT review_request_id, target_items, reason, review_memo, reviewer_id, created_at "
                               "FROM review_requests WHERE evaluation_id=%s ORDER BY created_at", (evaluation_id,))
        ]
        ev["progress_log"] = [{"stage": r["stage"], "at": iso(r["at"])}
                              for r in self.db.q("SELECT stage, at FROM progress_log WHERE evaluation_id=%s ORDER BY id", (evaluation_id,))]
        return ev

    # ── 공통 ──
    def _own_eval(self, claims: dict, evaluation_id: str, c=None) -> dict:
        if evaluation_id != claims["evaluation_id"]:
            raise forbidden(f"{evaluation_id}는 이 실행의 평가 건이 아닙니다")
        if c is not None:  # 트랜잭션 안: 행 잠금
            row = c.execute(f"SELECT {EVAL_COLS} FROM evaluations WHERE evaluation_id=%s FOR UPDATE", (evaluation_id,)).fetchone()
            if not row:
                raise not_found(f"평가 건 {evaluation_id}")
            ev = iso_row(row)
        else:
            ev = self.evaluation(evaluation_id)
        if ev["company_id"] != claims["company_id"]:
            raise forbidden()
        return ev

    @staticmethod
    def _latest_items(c, table: str, evaluation_id: str):
        r = c.execute(f"SELECT items FROM {table} WHERE evaluation_id=%s ORDER BY revision DESC LIMIT 1", (evaluation_id,)).fetchone()
        return r["items"] if r else None

    # ── save_evaluation_draft ──
    def save_evaluation_draft(self, claims: dict, evaluation_id: str, rubric_version: str, items: list, idempotency_key: str):
        need_key(idempotency_key)
        pool = self.store.evidence_pool(claims)
        with self.db.tx() as c:
            if (dup := replay(c, "save_evaluation_draft", idempotency_key)) is not None:
                return dup
            ev = self._own_eval(claims, evaluation_id, c)
            if ev["status"] not in WRITABLE:
                raise validation(f"평가 상태가 '{ev['status']}'라 초안을 저장할 수 없습니다")
            if rubric_version != ev["rubric_version"]:
                raise version_conflict(f"평가 건의 루브릭 버전은 {ev['rubric_version']}입니다 (요청 {rubric_version})")
            wt = self.store.raw[ev["work_id"]]["work_case"]["work_type"]
            rubric = self.store.rubric(wt, rubric_version)
            clean = check_items(items, rubric["items"], pool, self.store.masker, outside="스냅샷에 없는 근거 ID")
            row = c.execute(
                "INSERT INTO evaluation_drafts (evaluation_id, revision, items, run_id) "
                "SELECT %s, COALESCE(MAX(revision),0)+1, %s, %s FROM evaluation_drafts WHERE evaluation_id=%s RETURNING revision, saved_at",
                (evaluation_id, Jsonb(clean), claims["run_id"], evaluation_id)).fetchone()
            result = {"evaluation_id": evaluation_id, "draft_revision": row["revision"], "saved_at": iso(row["saved_at"]), "items_saved": len(clean)}
            remember(c, "save_evaluation_draft", idempotency_key, result, evaluation_id)
            return result

    # ── save_coaching ──
    def save_coaching(self, claims: dict, evaluation_id: str, coaching: list, idempotency_key: str):
        need_key(idempotency_key)
        pool = self.store.evidence_pool(claims)
        with self.db.tx() as c:
            if (dup := replay(c, "save_coaching", idempotency_key)) is not None:
                return dup
            ev = self._own_eval(claims, evaluation_id, c)
            if ev["status"] not in WRITABLE:
                raise validation(f"평가 상태가 '{ev['status']}'라 코칭을 저장할 수 없습니다")
            draft = self._latest_items(c, "evaluation_drafts", evaluation_id)
            if draft is None:
                raise validation("저장된 평가 초안이 없습니다. save_evaluation_draft를 먼저 호출하세요")
            clean = check_coaching(coaching, {i["item_id"] for i in draft}, pool, self.store.masker,
                                   not_saved="저장된 평가 항목이 아님", outside="근거 풀·스냅샷에 없음")
            row = c.execute(
                "INSERT INTO evaluation_coaching (evaluation_id, revision, items, run_id) "
                "SELECT %s, COALESCE(MAX(revision),0)+1, %s, %s FROM evaluation_coaching WHERE evaluation_id=%s RETURNING revision, saved_at",
                (evaluation_id, Jsonb(clean), claims["run_id"], evaluation_id)).fetchone()
            result = {"evaluation_id": evaluation_id, "coaching_revision": row["revision"], "saved_at": iso(row["saved_at"]), "items_saved": len(clean)}
            remember(c, "save_coaching", idempotency_key, result, evaluation_id)
            return result

    # ── request_review ──
    def request_review(self, claims: dict, evaluation_id: str, target_items: list, reason: str, review_memo: dict, idempotency_key: str):
        need_key(idempotency_key)
        pool = self.store.evidence_pool(claims)
        with self.db.tx() as c:
            if (dup := replay(c, "request_review", idempotency_key)) is not None:
                return dup
            ev = self._own_eval(claims, evaluation_id, c)
            if ev["status"] != "확인 대기":
                raise validation(f"검토 요청은 평가 상태가 '확인 대기'일 때만 가능합니다 (현재 '{ev['status']}')")
            wt = self.store.raw[ev["work_id"]]["work_case"]["work_type"]
            rubric = self.store.rubric(wt, ev["rubric_version"])
            name_of = {i["item_id"]: i["item"] for i in rubric["items"]}
            id_of = {v: k for k, v in name_of.items()}
            allowed: set[str] = set()
            appeal = self.store.human_action(claims, ev, "appeal")
            if appeal:
                allowed |= {id_of.get(t, t) for t in appeal.get("target_items", [])}
            draft = self._latest_items(c, "evaluation_drafts", evaluation_id)
            if draft:
                allowed |= {i["item_id"] for i in draft if i["status"] == "검토 필요"}
            if not target_items:
                raise validation("target_items가 필요합니다")
            norm = [id_of.get(t, t) for t in target_items]
            bad = [t for t, n in zip(target_items, norm) if n not in allowed]
            if bad:
                raise validation(f"{bad}는 이의 제기 항목이나 '검토 필요' 항목이 아닙니다 (허용: {sorted(allowed)})")
            if not reason:
                raise validation("reason이 필요합니다")
            check_text(self.store.masker, reason, "reason")
            memo = review_memo or {}
            unknown = [e for e in (memo.get("cited_evidence") or []) if e not in pool]
            if unknown:
                raise validation(f"review_memo.cited_evidence에 스냅샷에 없는 근거 ID {unknown}")
            check_text(self.store.masker, memo.get("comparison"), "review_memo.comparison")
            # 검토자는 업무 담당자 팀의 팀장(team_lead가 검토자 겸임). 팀장이 없으면 미배정
            team = self.store.raw[ev["work_id"]]["work_case"]["team_id"]
            reviewer = next((u["user_id"] for u in sorted(self.store.org["users"], key=lambda u: u["user_id"])
                             if u["role"] == "team_lead" and u["company_id"] == claims["company_id"]
                             and team is not None and u["team_id"] == team), None)
            rrid = new_id("RVQ")
            row = c.execute(
                "INSERT INTO review_requests (review_request_id, evaluation_id, target_items, reason, review_memo, reviewer_id, run_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING created_at",
                (rrid, evaluation_id, Jsonb(norm), reason, Jsonb(memo), reviewer, claims["run_id"])).fetchone()
            c.execute("UPDATE evaluations SET status='검토 대기', updated_at=now() WHERE evaluation_id=%s", (evaluation_id,))
            result = {"review_request_id": rrid, "evaluation_id": evaluation_id, "target_items": norm, "reviewer_id": reviewer,
                      "status": "검토 대기", "created_at": iso(row["created_at"])}
            remember(c, "request_review", idempotency_key, result, evaluation_id)
            return result

    # ── update_progress ──
    def update_progress(self, claims: dict, evaluation_id: str, stage: str):
        with self.db.tx() as c:
            ev = self._own_eval(claims, evaluation_id, c)
            if stage not in PROGRESS:
                raise validation(f"stage는 {PROGRESS} 중 하나입니다. {SERVER_ONLY}는 서버만 전환합니다")
            cur = ev["status"]
            if cur in SERVER_ONLY:
                raise validation(f"현재 상태 '{cur}'는 서버가 관리하는 상태라 진행 단계를 바꿀 수 없습니다")
            if cur in PROGRESS and PROGRESS.index(stage) < PROGRESS.index(cur):
                raise validation(f"진행 단계는 앞으로만 이동합니다 (현재 '{cur}' → 요청 '{stage}')")
            row = c.execute("UPDATE evaluations SET status=%s, updated_at=now() WHERE evaluation_id=%s RETURNING updated_at",
                            (stage, evaluation_id)).fetchone()
            c.execute("INSERT INTO progress_log (evaluation_id, stage, run_id) VALUES (%s,%s,%s)", (evaluation_id, stage, claims["run_id"]))
            return {"evaluation_id": evaluation_id, "previous": cur, "stage": stage, "updated_at": iso(row["updated_at"])}
