"""팀 평가(team, 계약 docs/team_evaluation.md) 도구·내부 API 검증. 실행 중인 두 MCP와 DB가 필요하다.

영업1팀 평가 건의 상태를 잠시 바꾸고(견적서 업무만 '확정', 나머지는 '처리 실패') 초안을 한 판 얹어 집계를 고정한 뒤,
끝나면 상태를 되돌리고 얹은 초안·만든 팀 평가 요청을 지운다. 깨끗한 DB에서 다른 verify 스크립트 뒤에 돌린다.
    python tests/verify_team.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Session, check, internal, issue_run, report  # noqa: E402
from harness import error_code as code  # noqa: E402

COMPANY, TEAM = "C-LGCNS-SALES", "T-SALES-01"
PERIOD = {"period_start": "2026-10-01", "period_end": "2026-11-01"}
CRITERIA = ["problem_definition", "instruction_quality", "verification", "security", "output_quality"]
# Agent 계약 모델(TeamSnapshot·TeamSaveReceipt, extra 금지)과 같은 칸
SNAPSHOT_KEYS = {"schema_version", "purpose", "request_id", "team_id", "snapshot_id", "period_start", "period_end",
                 "source_status", "deduplication", "privacy_policy_version", "min_members", "release_approved", "groups"}
GROUP_KEYS = {"work_type", "rubric_version", "rubric_sha256", "members", "finalized_evaluations", "cells"}
RECEIPT_KEYS = {"request_id", "snapshot_id", "report_sha256", "revision", "status"}
# 견적서 업무 순서대로 5개 항목 점수. 결과 검증(3번째)은 앞의 담당자 일부만 채점해 공개 기준(5명) 미달로 만든다
PATTERNS = [[2, 2, 2, 2, 1], [2, 1, 2, 2, 1], [1, 1, 2, 0, 2], [2, 0, 2, 2, 1], [2, 1, None, 2, 2],
            [1, 0, None, 2, 1], [2, 1, None, 2, 1], [2, 2, None, 0, 2], [1, 1, None, 2, 2]]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def setup(conn) -> tuple[dict, dict]:
    """영업1팀 평가 건 상태를 바꾸고 견적서 업무마다 최신 평가 하나에 초안을 얹는다. (원래 상태, 기대 집계)."""
    rows = conn.execute(
        "SELECT e.evaluation_id, e.status, e.work_id, w.work_type, w.employee_id FROM evaluations e JOIN work_cases w USING (work_id) "
        "WHERE w.team_id=%s ORDER BY e.work_id, e.evaluation_id", (TEAM,)).fetchall()
    original = {r[0]: r[1] for r in rows}
    latest: dict[str, tuple] = {}
    for eid, _, wid, wt, emp in rows:
        if wt == "quote":
            latest[wid] = (eid, emp)          # 업무마다 평가 건 ID가 가장 큰 것 하나만 확정으로 둔다
    rubric = conn.execute("SELECT item_id, item FROM rubric_items WHERE doc_id='RUB-quote' AND version='v2' ORDER BY seq").fetchall()
    conn.execute("UPDATE evaluations SET status='처리 실패' WHERE evaluation_id = ANY(%s)", (list(original),))
    expected = {c: {"members": set(), "n": 0, "sum": 0} for c in CRITERIA}
    members = set()
    for (wid, (eid, emp)), scores in zip(sorted(latest.items()), PATTERNS):
        members.add(emp)
        draft = []
        for (item_id, item), crit, score in zip(rubric, CRITERIA, scores):
            draft.append({"item_id": item_id, "item": item, "status": "채점" if score is not None else "미관측",
                          "score": score, "reason": "", "evidence_ids": []})
            if score is not None:
                expected[crit]["members"].add(emp)
                expected[crit]["n"] += 1
                expected[crit]["sum"] += score
        conn.execute("INSERT INTO evaluation_drafts (evaluation_id, revision, items, run_id) "
                     "SELECT %s, coalesce(max(revision), 0) + 1, %s, 'VERIFY-TEAM' FROM evaluation_drafts WHERE evaluation_id=%s",
                     (eid, Jsonb(draft), eid))
        conn.execute("UPDATE evaluations SET status='확정' WHERE evaluation_id=%s", (eid,))
    return original, {"members": len(members), "evaluations": len(latest), "cells": expected}


def cleanup(conn, original: dict) -> None:
    conn.execute("DELETE FROM evaluation_drafts WHERE run_id='VERIFY-TEAM'")
    for eid, status in original.items():
        conn.execute("UPDATE evaluations SET status=%s WHERE evaluation_id=%s", (status, eid))
    conn.execute("DELETE FROM team_evaluations WHERE company_id=%s", (COMPANY,))


def withheld_report(snap: dict, sha: str, summary: str = "공개 가능한 집계 근거가 없어 팀 평가와 코칭을 보류합니다.") -> dict:
    return {"schema_version": "1", "purpose": "team", "request_id": snap["request_id"], "team_id": snap["team_id"],
            "snapshot_id": snap["snapshot_id"], "snapshot_sha256": sha, "period_start": snap["period_start"],
            "period_end": snap["period_end"], "status": "withheld", "presentation_version": "team-coaching-v1",
            "weighting": "per_evaluation_within_rubric", "summary": summary, "metrics": [], "strengths": [],
            "improvements": [], "limitations": ["개인의 역량·순위·인사평가를 나타내지 않습니다."]}


def save_args(snap: dict, rep: dict, **over) -> dict:
    return {"request_id": snap["request_id"], "snapshot_id": snap["snapshot_id"], "idempotency_key": "a" * 64,
            "report_sha256": digest(rep), "report": rep, **over}


async def team_checks(expected: dict) -> None:
    r = internal("POST", "/internal/team-evaluations", {"company_id": COMPANY, "team_id": TEAM, **PERIOD})
    check("release_approved 없이 요청 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
    r = internal("POST", "/internal/team-evaluations", {"company_id": COMPANY, "team_id": "T-NONE", "release_approved": True, **PERIOD})
    check("없는 팀 → NOT_FOUND", code(r) == "NOT_FOUND", str(r)[:200])
    r = internal("POST", "/internal/team-evaluations", {"company_id": COMPANY, "team_id": TEAM, "release_approved": True,
                                                         "period_start": "2026-10-10", "period_end": "2026-10-10"})
    check("빈 기간 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])

    rid = str(uuid.uuid4())
    body = {"company_id": COMPANY, "team_id": TEAM, "release_approved": True, "request_id": rid, "requested_by": "L-3001",
            "min_members": 5, **PERIOD}   # 시드 회사는 2명 기준. 이 검증 데이터는 5명 기준으로 짰다
    r = internal("POST", "/internal/team-evaluations", body)
    ok = check("요청 생성 → created:true, 대기", r.get("ok") and r["data"]["created"] and r["data"]["status"] == "대기", str(r)[:300])
    if not ok:
        return
    made = r["data"]
    snap = made["snapshot"]
    check("스냅샷 칸이 TeamSnapshot과 같음", set(snap) == SNAPSHOT_KEYS, str(sorted(snap)))
    check("스냅샷 해시 = 키 정렬 JSON SHA-256", made["snapshot_sha256"] == digest(snap))
    check("팀 ID는 UUID(팀 코드 비노출)", str(uuid.UUID(snap["team_id"])) == snap["team_id"] and TEAM not in json.dumps(snap))
    check("고정 값: finalized·latest_final_per_work·min_members 5",
          (snap["source_status"], snap["deduplication"], snap["min_members"]) == ("finalized", "latest_final_per_work", 5), str(snap)[:200])
    groups = snap["groups"]
    check("견적서 그룹 하나만(다른 유형은 확정 아님)", [g["work_type"] for g in groups] == ["quote"], str(groups)[:300])
    if groups:
        g = groups[0]
        check("그룹 칸", set(g) == GROUP_KEYS and g["rubric_version"] == "v2" and len(g["rubric_sha256"]) == 64, str(g)[:200])
        check(f"구성원 {expected['members']}명·확정 {expected['evaluations']}건",
              (g["members"], g["finalized_evaluations"]) == (expected["members"], expected["evaluations"]), str(g)[:200])
        cells = {c["criterion"]: c["statistics"] for c in g["cells"]}
        check("항목 순서 고정 5개", [c["criterion"] for c in g["cells"]] == CRITERIA)
        for crit, e in expected["cells"].items():
            want = ({"scored_members": len(e["members"]), "scored_evaluations": e["n"], "score_sum": e["sum"]}
                    if len(e["members"]) >= 5 else None)
            check(f"{crit} 집계 {want}", cells.get(crit) == want, str(cells.get(crit)))
    check("이름·평가 건 ID 비노출", "EVAL-" not in json.dumps(snap) and "E-10" not in json.dumps(snap, ensure_ascii=False))

    again = internal("POST", "/internal/team-evaluations", body)
    check("같은 요청 재전송 → created:false, 같은 스냅샷",
          again.get("ok") and not again["data"]["created"] and again["data"]["snapshot_id"] == snap["snapshot_id"], str(again)[:200])
    r = internal("POST", "/internal/team-evaluations", {**body, "release_approved": False})
    check("같은 요청 ID에 다른 범위 → VERSION_CONFLICT", code(r) == "VERSION_CONFLICT", str(r)[:200])
    r = internal("POST", "/internal/team-runs", {"request_id": str(uuid.uuid4())})
    check("없는 요청의 팀 토큰 → NOT_FOUND", code(r) == "NOT_FOUND", str(r)[:200])

    run = internal("POST", "/internal/team-runs", {"request_id": rid})["data"]
    check("팀 토큰 kind=team", run["kind"] == "team" and run["request_id"] == rid)
    async with Session(run["run_token"]) as s:
        got = await s.call("business", "get_team_evaluation_snapshot", {"request_id": rid})
        check("팀 토큰 → 고정 스냅샷 그대로", got["ok"] and got["data"] == snap, str(got)[:200])
        check("meta.snapshot = 요청·스냅샷 고정 문맥",
              got.get("meta", {}).get("snapshot") == {"request_id": rid, "snapshot_id": snap["snapshot_id"],
                                                      "snapshot_sha256": made["snapshot_sha256"]}, str(got.get("meta")))
        r = await s.call("business", "get_team_evaluation_snapshot", {"request_id": str(uuid.uuid4())})
        check("다른 요청 ID → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])
        r = await s.call("business", "get_work_case", {"work_id": "W-E2E-QT-01"})
        check("팀 토큰으로 개인 업무 조회 → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])
        r = await s.call("rag", "search_policy", {"query": "할인"})
        check("팀 토큰으로 rag 도구 → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])
        tampered = {**withheld_report(snap, made["snapshot_sha256"])}
        r = await s.call("business", "save_team_evaluation_report", save_args(snap, tampered))
        check("공개 지표가 있는데 지표를 비운 리포트 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
    run_eval = issue_run("EVAL-E2E-QT-01-v2")
    async with Session(run_eval["run_token"]) as s:
        r = await s.call("business", "get_team_evaluation_snapshot", {"request_id": rid})
        check("최종 평가 토큰으로 팀 스냅샷 → FORBIDDEN", code(r) == "FORBIDDEN", str(r)[:200])

    # 공개 승인 전 요청: 집계 없음 → 보류 리포트 저장·재전송·충돌
    wid = str(uuid.uuid4())
    w = internal("POST", "/internal/team-evaluations", {**body, "request_id": wid, "release_approved": False})["data"]
    wsnap = w["snapshot"]
    check("공개 승인 전 → groups=[]", wsnap["groups"] == [] and wsnap["release_approved"] is False, str(wsnap)[:200])
    rep = withheld_report(wsnap, w["snapshot_sha256"])
    run = internal("POST", "/internal/team-runs", {"request_id": wid})["data"]
    async with Session(run["run_token"]) as s:
        r = await s.call("business", "save_team_evaluation_report", save_args(wsnap, rep, report_sha256="0" * 64))
        check("report_sha256 불일치 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
        r = await s.call("business", "save_team_evaluation_report", save_args(wsnap, rep, snapshot_id=str(uuid.uuid4())))
        check("다른 snapshot_id → VERSION_CONFLICT", code(r) == "VERSION_CONFLICT", str(r)[:200])
        bad = withheld_report(wsnap, w["snapshot_sha256"], summary="담당자 010-1234-5678로 문의")
        r = await s.call("business", "save_team_evaluation_report", save_args(wsnap, bad))
        check("문장에 연락처 → VALIDATION_ERROR", code(r) == "VALIDATION_ERROR", str(r)[:200])
        saved = await s.call("business", "save_team_evaluation_report", save_args(wsnap, rep))
        check("보류 리포트 저장 → 영수증", saved["ok"] and set(saved["data"]) == RECEIPT_KEYS
              and saved["data"]["status"] == "saved" and saved["data"]["revision"] == 1, str(saved)[:300])
        dup = await s.call("business", "save_team_evaluation_report", save_args(wsnap, rep))
        check("같은 키·같은 내용 재전송 → 처음 영수증(duplicate)",
              dup["ok"] and dup["meta"].get("duplicate") is True and dup["data"] == saved.get("data"), str(dup)[:200])
        other = withheld_report(wsnap, w["snapshot_sha256"], summary="다른 문장입니다.")
        r = await s.call("business", "save_team_evaluation_report", save_args(wsnap, other))
        check("같은 요청에 다른 내용 → VERSION_CONFLICT", code(r) == "VERSION_CONFLICT", str(r)[:200])
    d = internal("GET", f"/internal/team-evaluations/{wid}")
    check("내부 조회: 완료·저장한 리포트", d.get("ok") and d["data"]["status"] == "완료" and d["data"]["report"] == rep, str(d)[:200])
    d = internal("GET", f"/internal/team-evaluations/{rid}")
    check("내부 조회: 저장 전은 대기·report null", d.get("ok") and d["data"]["status"] == "대기" and d["data"]["report"] is None)


async def main() -> int:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        original, expected = setup(conn)
        try:
            await team_checks(expected)
        finally:
            cleanup(conn, original)
            left = conn.execute("SELECT count(*) FROM evaluation_drafts WHERE run_id='VERIFY-TEAM'").fetchone()[0]
            check("정리: 얹은 초안 삭제·평가 상태 복구", left == 0 and dict(conn.execute(
                "SELECT evaluation_id, status FROM evaluations WHERE evaluation_id = ANY(%s)", (list(original),)).fetchall()) == original)
    return report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
