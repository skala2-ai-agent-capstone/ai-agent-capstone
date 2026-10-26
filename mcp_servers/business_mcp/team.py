"""팀 업무 평가: 확정 평가의 팀 집계 스냅샷과 팀 리포트 저장 (team_evaluations). 계약 정본: docs/team_evaluation.md

백엔드가 내부 API로 요청 행을 만들면 그때 집계를 한 번 계산해 스냅샷으로 고정하고, 팀 Agent가 팀 토큰으로
get_team_evaluation_snapshot → save_team_evaluation_report 순서로 부른다.
- 집계: [period_start, period_end)(KST)에 완료된 팀 업무마다 최신 '확정' 평가 하나. 업무 유형·기준 버전·기준 내용 해시로 묶고,
  최신 초안에서 '채점'이고 점수가 유효한 항목만 합계·분모에 넣는다(미관측·검토 필요는 빠진다). 구성원 수는 서로 다른 담당자 수다.
- 기준 항목은 이름으로 계약의 공통 5개 항목에 맞춘다. 맞지 않는 기준이나 초안이 섞이면 집계하지 않고 VALIDATION_ERROR.
- 공개: 구성원이 min_members(2 이상, 회사 min_aggregation_size 이상) 미만인 그룹은 빼고, 항목 관측 구성원이 미만이면 statistics=null.
  공개 승인(release_approved)은 백엔드가 정해 보낸다. false면 groups=[].
- Agent에는 팀 ID 대신 회사·팀으로 만든 고정 UUID를 준다. 이름·평가 건 ID·개별 점수는 스냅샷에 넣지 않는다.
- 저장: 요청 하나에 리포트 하나. 요청·스냅샷·해시와 지표 수치를 고정한 스냅샷으로 다시 계산해 대조한다.
  같은 키·같은 내용은 처음 영수증(duplicate), 다른 내용은 VERSION_CONFLICT. 다시 만들려면 새 요청을 만든다.
응답 data는 Agent 계약 모델(TeamSnapshot·TeamSaveReceipt, 모르는 칸 거부)과 칸이 정확히 같아야 한다.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from common.runtime import TEAM, Result, forbidden, new_id, not_found, validation, version_conflict

from .checks import check_text
from .idempotency import need_key
from .pg import Db, iso
from .store import Store

KST = timezone(timedelta(hours=9))
# 기준 항목 이름 → 계약의 공통 항목 (save_rubric이 이 다섯 이름을 강제한다). 순서가 스냅샷 cells 순서다
CRITERIA = {"문제 정의": "problem_definition", "지시 품질": "instruction_quality", "결과 검증": "verification",
            "보안 준수": "security", "결과물 품질": "output_quality"}
# 개선 활동 → 대상 항목 (Agent evaluation_agent/team/report.py ACTIONS와 같아야 한다)
ACTION_CRITERION = {
    "define_acceptance": "problem_definition", "clarify_constraints": "problem_definition",
    "structure_prompt": "instruction_quality", "request_evidence": "instruction_quality",
    "cross_check": "verification", "review_assumptions": "verification",
    "mask_before_input": "security", "check_sharing_scope": "security",
    "use_review_checklist": "output_quality", "validate_deliverable": "output_quality",
}
MIN_MEMBERS = 2                      # 계약 하한(시연 데이터 규모). 실제 공개 기준은 회사 min_aggregation_size로 올린다
MAX_MEMBERS = 100                    # 계약 min_members 상한
MAX_DAYS = 366
MAX_GROUPS = 100
PRIVACY_POLICY = "team-v1-provisional"
TEAM_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:ax-eval:team")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TEXT_MAX, LIMITATIONS_MAX, INSIGHTS_MAX = 2000, 20, 3

REPORT_KEYS = {"schema_version", "purpose", "request_id", "team_id", "snapshot_id", "snapshot_sha256", "period_start",
               "period_end", "status", "presentation_version", "weighting", "summary", "metrics", "strengths",
               "improvements", "limitations"}
INSIGHT_KEYS = {"metric_id", "observation", "action_id", "recommendation"}
TEAM_COLS = ("request_id, snapshot_id, company_id, team_id, period_start, period_end, requested_by, requested_at, "
             "release_approved, min_members, snapshot_sha256, status, report_sha256, revision, idempotency_key, saved_at")


def digest(value) -> str:
    """Agent content_hash와 같은 규칙: 키를 정렬한 JSON(ensure_ascii=False, 기본 구분자)의 SHA-256."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def team_uuid(company_id: str, team_id: str) -> str:
    """Agent에 주는 팀 ID. 같은 팀은 늘 같은 값이고 팀 이름·ID를 드러내지 않는다."""
    return str(uuid.uuid5(TEAM_NS, f"{company_id}/{team_id}"))


def _uuid(v, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        raise validation(f"{name}는 UUID여야 합니다")


def _date(v, name: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise validation(f"{name}는 YYYY-MM-DD 날짜여야 합니다")


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=KST)


def metrics_for(snapshot: dict) -> list[dict]:
    """스냅샷의 공개 지표. Agent metrics_for와 같은 규칙(그룹 정렬 번호, 평균 3자리, 관측 비율 1자리, 1.5/2 이상 강점)."""
    out = []
    groups = sorted(snapshot["groups"], key=lambda g: (g["work_type"], g["rubric_version"], g["rubric_sha256"]))
    for index, g in enumerate(groups, 1):
        for cell in sorted(g["cells"], key=lambda c: c["criterion"]):
            s = cell["statistics"]
            if s is None:
                continue
            out.append({
                "metric_id": f"g{index}.{cell['criterion']}", "work_type": g["work_type"], "rubric_version": g["rubric_version"],
                "rubric_sha256": g["rubric_sha256"], "criterion": cell["criterion"], "score_sum": s["score_sum"],
                "scored_evaluations": s["scored_evaluations"], "finalized_evaluations": g["finalized_evaluations"],
                "mean_score": round(s["score_sum"] / s["scored_evaluations"], 3),
                "coverage_percent": round(100 * s["scored_evaluations"] / g["finalized_evaluations"], 1),
                "band": "strength" if s["score_sum"] * 2 >= s["scored_evaluations"] * 3 else "improvement",
            })
    return out


def _text(v, where: str, masker) -> None:
    if not isinstance(v, str) or not v.strip() or len(v) > TEXT_MAX:
        raise validation(f"{where}는 1~{TEXT_MAX}자 문자열이어야 합니다")
    check_text(masker, v, where)


def check_report(report: dict, snapshot: dict, snapshot_sha256: str, masker) -> None:
    """리포트를 고정한 스냅샷과 대조한다. 수치·지표 ID·항목·활동은 다시 계산해 같아야 하고 문장은 형식·민감정보만 본다."""
    if set(report) != REPORT_KEYS:
        raise validation("report 칸이 팀 리포트 계약(TeamReport)과 다릅니다")
    fixed = {"schema_version": "1", "purpose": "team", "presentation_version": "team-coaching-v1",
             "weighting": "per_evaluation_within_rubric", "snapshot_sha256": snapshot_sha256,
             **{k: snapshot[k] for k in ("request_id", "team_id", "snapshot_id", "period_start", "period_end")}}
    wrong = [k for k, v in fixed.items() if report[k] != v]
    if wrong:
        raise validation(f"report의 {', '.join(wrong)}이(가) 고정한 스냅샷과 다릅니다")
    metrics = metrics_for(snapshot)
    if report["metrics"] != metrics:
        raise validation("report.metrics가 스냅샷 집계로 계산한 값과 다릅니다")
    status = "generated" if metrics else ("insufficient_data" if snapshot["release_approved"] else "withheld")
    if report["status"] != status:
        raise validation(f"report.status는 '{status}'여야 합니다")
    by_id = {m["metric_id"]: m for m in metrics}
    seen: set[str] = set()
    for band, key in (("strength", "strengths"), ("improvement", "improvements")):
        rows = report[key]
        if not isinstance(rows, list) or len(rows) > INSIGHTS_MAX:
            raise validation(f"report.{key}는 최대 {INSIGHTS_MAX}개 목록이어야 합니다")
        for row in rows:
            if not isinstance(row, dict) or set(row) != INSIGHT_KEYS or not isinstance(row["metric_id"], str):
                raise validation(f"report.{key} 항목 칸이 계약과 다릅니다")
            m = by_id.get(row["metric_id"])
            if m is None or row["metric_id"] in seen or m["band"] != band:
                raise validation(f"report.{key}의 {row['metric_id']}는 공개 지표가 아니거나 중복이거나 {band} 지표가 아닙니다")
            seen.add(row["metric_id"])
            _text(row["observation"], f"report.{key}.observation", masker)
            if band == "strength":
                if row["action_id"] is not None or row["recommendation"] is not None:
                    raise validation("강점에는 action_id·recommendation을 넣지 않습니다")
            else:
                if not isinstance(row["action_id"], str) or ACTION_CRITERION.get(row["action_id"]) != m["criterion"]:
                    raise validation(f"개선 활동 {row['action_id']}는 {m['criterion']} 항목의 활동이 아닙니다")
                _text(row["recommendation"], "report.improvements.recommendation", masker)
    _text(report["summary"], "report.summary", masker)
    if not isinstance(report["limitations"], list) or len(report["limitations"]) > LIMITATIONS_MAX:
        raise validation(f"report.limitations는 최대 {LIMITATIONS_MAX}개 목록이어야 합니다")
    for text in report["limitations"]:
        _text(text, "report.limitations", masker)


class TeamStore:
    def __init__(self, db: Db, store: Store):
        self.db = db
        self.store = store

    # ── 백엔드 역할(내부 API) ──
    def create_request(self, body: dict) -> tuple[dict, bool]:
        """요청 행을 만들고 집계 스냅샷을 고정한다. 같은 request_id·같은 범위면 기존 행, 다른 범위면 VERSION_CONFLICT.
        body: {company_id, team_id, period_start, period_end(미포함), release_approved, request_id?, requested_by?, min_members?}."""
        company, team = body.get("company_id"), body.get("team_id")
        released = body.get("release_approved")
        if not (company and team):
            raise validation("company_id와 team_id가 필요합니다")
        if not isinstance(released, bool):
            raise validation("release_approved(공개 승인 여부 true|false)가 필요합니다")
        start, end = _date(body.get("period_start"), "period_start"), _date(body.get("period_end"), "period_end")
        if not start < end or (end - start).days > MAX_DAYS:
            raise validation(f"기간은 period_start < period_end(미포함)이고 최대 {MAX_DAYS}일입니다")
        asked = body.get("min_members")
        if asked is not None and (type(asked) is not int or not MIN_MEMBERS <= asked <= MAX_MEMBERS):
            raise validation(f"min_members는 {MIN_MEMBERS}~{MAX_MEMBERS} 정수입니다")
        rid = _uuid(body["request_id"], "request_id") if body.get("request_id") is not None else uuid.uuid4()
        with self.db.tx() as c:
            t = c.execute("SELECT c.min_aggregation_size FROM teams t JOIN companies c USING (company_id) "
                          "WHERE t.team_id=%s AND t.company_id=%s", (team, company)).fetchone()
            if not t:
                raise not_found(f"회사 {company}의 팀 {team}")
            user = body.get("requested_by")
            if user is not None and not c.execute("SELECT 1 FROM users WHERE user_id=%s AND company_id=%s", (user, company)).fetchone():
                raise not_found(f"회사 {company}의 사용자 {user}")
            min_members = max(MIN_MEMBERS, t["min_aggregation_size"], asked or 0)
            if min_members > MAX_MEMBERS:
                raise validation(f"회사 최소 집계 인원 {min_members}명은 계약 상한 {MAX_MEMBERS}명을 넘습니다")
            old = c.execute(f"SELECT {TEAM_COLS}, snapshot FROM team_evaluations WHERE request_id=%s", (rid,)).fetchone()
            if old:
                if (old["company_id"], old["team_id"], old["period_start"], old["period_end"], old["release_approved"],
                        old["min_members"]) != (company, team, start, end, released, min_members):
                    raise version_conflict(f"팀 평가 요청 {rid}는 다른 범위로 이미 만들어졌습니다")
                return self._summary(old, snapshot=old["snapshot"]), False
            sid = uuid.uuid4()
            groups, evaluation_ids = self._aggregate(c, company, team, start, end, min_members)
            snapshot = {
                "schema_version": "1", "purpose": "team", "request_id": str(rid), "team_id": team_uuid(company, team),
                "snapshot_id": str(sid), "period_start": start.isoformat(), "period_end": end.isoformat(),
                "source_status": "finalized", "deduplication": "latest_final_per_work",
                "privacy_policy_version": PRIVACY_POLICY, "min_members": min_members, "release_approved": released,
                "groups": groups if released else [],
            }
            row = c.execute(
                "INSERT INTO team_evaluations (request_id, snapshot_id, company_id, team_id, period_start, period_end, requested_by, "
                "release_approved, min_members, snapshot, snapshot_sha256, source_evaluation_ids) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {TEAM_COLS}",
                (rid, sid, company, team, start, end, body.get("requested_by"), released, min_members, Jsonb(snapshot),
                 digest(snapshot), evaluation_ids)).fetchone()
        return self._summary(row, snapshot=snapshot), True

    def _aggregate(self, c, company: str, team: str, start: date, end: date, min_members: int) -> tuple[list[dict], list[str]]:
        """업무마다 최신 확정 평가 하나로 그룹 집계를 만든다. 공개 기준 미달 그룹·항목은 여기서 뺀다(groups, 쓴 평가 건 ID)."""
        rows = c.execute("""
            SELECT DISTINCT ON (e.work_id) e.evaluation_id, e.rubric_version, w.work_type, w.employee_id,
                   (SELECT d.items FROM evaluation_drafts d WHERE d.evaluation_id = e.evaluation_id
                     ORDER BY d.revision DESC LIMIT 1) AS items
              FROM evaluations e JOIN work_cases w ON w.work_id = e.work_id
             WHERE e.company_id = %s AND w.team_id = %s AND e.status = '확정' AND w.work_type IS NOT NULL
               AND w.completed_at >= %s AND w.completed_at < %s
             ORDER BY e.work_id, e.created_at DESC, e.evaluation_id DESC
        """, (company, team, _at(start), _at(end))).fetchall()
        rubrics: dict[tuple[str, str], dict] = {}
        groups: dict[tuple[str, str, str], dict] = {}
        for r in rows:
            key = (r["work_type"], r["rubric_version"])
            if key not in rubrics:
                rubrics[key] = self._rubric(c, company, *key)
            rub = rubrics[key]
            g = groups.setdefault((*key, rub["sha256"]), {
                "members": set(), "evaluations": 0, "cells": {k: {"members": set(), "n": 0, "sum": 0} for k in CRITERIA.values()}})
            g["members"].add(r["employee_id"])
            g["evaluations"] += 1
            seen: set[str] = set()
            for item in r["items"] or []:
                crit = rub["criteria"].get(item.get("item_id"))
                if crit is None or crit in seen:
                    raise validation(f"평가 건 {r['evaluation_id']}의 초안 항목 {item.get('item_id')}을(를) 기준 항목에 맞출 수 없습니다")
                seen.add(crit)
                score = item.get("score")
                if item.get("status") != "채점" or type(score) is not int or score not in ((0, 2) if crit == "security" else (0, 1, 2)):
                    continue
                cell = g["cells"][crit]
                cell["members"].add(r["employee_id"])
                cell["n"] += 1
                cell["sum"] += score
        out = []
        for (work_type, version, sha), g in sorted(groups.items()):
            if len(g["members"]) < min_members:
                continue
            cells = []
            for crit in CRITERIA.values():
                cell = g["cells"][crit]
                stats = ({"scored_members": len(cell["members"]), "scored_evaluations": cell["n"], "score_sum": cell["sum"]}
                         if len(cell["members"]) >= min_members else None)
                cells.append({"criterion": crit, "statistics": stats})
            out.append({"work_type": work_type, "rubric_version": version, "rubric_sha256": sha, "members": len(g["members"]),
                        "finalized_evaluations": g["evaluations"], "cells": cells})
        if len(out) > MAX_GROUPS:
            raise validation(f"집계 그룹이 {MAX_GROUPS}개를 넘습니다. 기간을 줄이세요")
        return out, [r["evaluation_id"] for r in rows]

    @staticmethod
    def _rubric(c, company: str, work_type: str, version: str) -> dict:
        """평가에 쓴 기준 버전의 내용 해시와 기준 항목 ID → 공통 항목. 계약에 맞출 수 없는 기준이면 VALIDATION_ERROR."""
        if not VERSION_RE.match(version or ""):
            raise validation(f"평가 기준 버전 '{version}'은 팀 집계 형식이 아닙니다")
        docs = c.execute("SELECT doc_id FROM documents WHERE collection='rubrics' AND company_id=%s AND work_type=%s AND version=%s",
                         (company, work_type, version)).fetchall()
        if len(docs) != 1:
            raise validation(f"{work_type} 평가 기준 {version}을(를) 하나로 찾을 수 없습니다 ({len(docs)}건)")
        doc_id = docs[0]["doc_id"]
        items = c.execute(
            "SELECT item_id, item, criterion, score_2, score_1, score_0, unobserved, needs_review, evidence_sources, related_clauses "
            "FROM rubric_items WHERE doc_id=%s AND version=%s ORDER BY seq", (doc_id, version)).fetchall()
        if sorted(i["item"] for i in items) != sorted(CRITERIA):
            raise validation(f"평가 기준 {doc_id} {version}의 항목이 공통 5개 항목({', '.join(CRITERIA)})과 다릅니다")
        return {"sha256": digest({"doc_id": doc_id, "version": version, "items": items}),
                "criteria": {i["item_id"]: CRITERIA[i["item"]] for i in items}}

    @staticmethod
    def _summary(row: dict, snapshot: dict | None = None) -> dict:
        out = {k: str(v) if isinstance(v, uuid.UUID) else iso(v) for k, v in row.items() if k not in ("snapshot", "idempotency_key")}
        if snapshot is not None:
            out["snapshot"] = snapshot
        return out

    def get(self, request_id: str) -> dict:
        """요청 상태·스냅샷·리포트 (관리자 화면용)."""
        row = self.db.one(f"SELECT {TEAM_COLS}, snapshot, report FROM team_evaluations WHERE request_id=%s",
                          (_uuid(request_id, "request_id"),))
        if not row:
            raise not_found(f"팀 평가 요청 {request_id}")
        return {**self._summary(row, snapshot=row["snapshot"]), "report": row["report"]}

    def run_claims(self, body: dict) -> dict:
        """팀 평가 실행 토큰(kind=team)의 claims. 요청 하나에 묶이고 meta.snapshot은 그 요청의 고정 스냅샷이다."""
        rid = _uuid(body.get("request_id"), "request_id")
        row = self.db.one("SELECT company_id, team_id, snapshot_id, snapshot_sha256 FROM team_evaluations WHERE request_id=%s", (rid,))
        if not row:
            raise not_found(f"팀 평가 요청 {rid}")
        return {"kind": TEAM, "run_id": new_id("RUN"), "company_id": row["company_id"], "team_id": row["team_id"],
                "request_id": str(rid), "work_id": "",
                "snapshot": {"request_id": str(rid), "snapshot_id": str(row["snapshot_id"]), "snapshot_sha256": row["snapshot_sha256"]}}

    # ── 팀 Agent 도구(team 토큰) ──
    def _own(self, claims: dict, request_id: str, c=None) -> dict:
        rid = _uuid(request_id, "request_id")
        if str(rid) != claims.get("request_id"):
            raise forbidden(f"{request_id}는 이 실행의 팀 평가 요청이 아닙니다")
        sql = f"SELECT {TEAM_COLS}, snapshot FROM team_evaluations WHERE request_id=%s"
        row = c.execute(sql + " FOR UPDATE", (rid,)).fetchone() if c is not None else self.db.one(sql, (rid,))
        if not row:
            raise not_found(f"팀 평가 요청 {request_id}")
        if (row["company_id"], row["team_id"]) != (claims["company_id"], claims.get("team_id")):
            raise forbidden()
        return row

    def snapshot(self, claims: dict, request_id: str) -> dict:
        return self._own(claims, request_id)["snapshot"]

    def save(self, claims: dict, request_id: str, snapshot_id: str, idempotency_key: str, report_sha256: str, report: dict):
        need_key(idempotency_key)
        for name, v in (("idempotency_key", idempotency_key), ("report_sha256", report_sha256)):
            if not isinstance(v, str) or not DIGEST_RE.match(v):
                raise validation(f"{name}는 소문자 16진수 64자(SHA-256)여야 합니다")
        if not isinstance(report, dict):
            raise validation("report는 객체여야 합니다")
        if digest(report) != report_sha256:
            raise validation("report_sha256이 report 내용의 해시와 다릅니다")
        with self.db.tx() as c:
            row = self._own(claims, request_id, c)
            if str(_uuid(snapshot_id, "snapshot_id")) != str(row["snapshot_id"]):
                raise version_conflict("이 팀 평가 요청에 고정한 스냅샷이 아닙니다")
            if row["status"] == "완료":
                if (row["idempotency_key"], row["report_sha256"]) == (idempotency_key, report_sha256):
                    return Result(self._receipt(row), duplicate=True)
                raise version_conflict("이 요청의 팀 리포트는 이미 다른 내용으로 저장됐습니다. 다시 만들려면 새 팀 평가 요청을 만드세요")
            check_report(report, row["snapshot"], row["snapshot_sha256"], self.store.masker)
            saved = c.execute(
                "UPDATE team_evaluations SET status='완료', report=%s, report_sha256=%s, revision=1, idempotency_key=%s, "
                f"saved_at=now(), run_id=%s WHERE request_id=%s RETURNING {TEAM_COLS}",
                (Jsonb(report), report_sha256, idempotency_key, claims["run_id"], row["request_id"])).fetchone()
        return self._receipt(saved)

    @staticmethod
    def _receipt(row: dict) -> dict:
        return {"request_id": str(row["request_id"]), "snapshot_id": str(row["snapshot_id"]), "report_sha256": row["report_sha256"],
                "revision": row["revision"], "status": "saved"}
