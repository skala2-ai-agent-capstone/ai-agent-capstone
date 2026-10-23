"""팀 평가 MCP 저장소(business_mcp.team)의 실제 PostgreSQL 집계·저장과 Agent 팀 계약 검사.

FEEDBACK_TEST_DATABASE_URL이 지정된 테스트 DB 안에 임의 스키마를 만들고 종료 시 삭제한다
(test_feedback_postgres.db). 서버 스냅샷을 Agent TeamSnapshot으로 읽고,
Agent가 만든 리포트를 서버가 저장·재검증하는지 본다.
"""

import os
from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ai_agents.evaluation_agent.team.report import ACTIONS, build_report, metrics_for
from ai_agents.orchestration.team_evaluation import TeamEvaluationWorkflow, build_save_request
from ai_agents.shared.team_contracts import TeamDecision, TeamSaveReceipt, TeamSnapshot
from ai_agents.shared.workflow_models import content_hash

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

db = import_module("test_feedback_postgres").db

pytestmark = pytest.mark.skipif(
    not os.getenv("FEEDBACK_TEST_DATABASE_URL"), reason="테스트용 PostgreSQL 미지정"
)
KST = timezone(timedelta(hours=9))
ITEMS = ["문제 정의", "지시 품질", "결과 검증", "보안 준수", "결과물 품질"]
OCT = {"company_id": "C-TEST", "team_id": "T-A", "period_start": "2026-10-01",
       "period_end": "2026-11-01", "release_approved": True}
MASKING = {"rules": [
    {"rule_id": "MASK-PHONE", "kind": "masking", "pattern": r"01[016789]-?\d{3,4}-?\d{4}",
     "replace_with": "[연락처]"},
]}


def mcp(name):
    return import_module(name)


@pytest.fixture
def team(db):
    """팀 T-A(구성원 6명)와 T-B, 견적서 기준 v2를 넣은 TeamStore."""
    c = db.conn
    c.execute("INSERT INTO companies VALUES ('C-OTHER', '다른 회사', 5)")
    c.execute("INSERT INTO teams VALUES ('T-A', 'C-TEST', '영업1팀'), "
              "('T-B', 'C-TEST', '영업2팀'), ('T-X', 'C-OTHER', '다른 팀')")
    for n in range(1, 8):
        c.execute("INSERT INTO users (user_id, employee_no, password, company_id, team_id, "
                  "name, role) VALUES (%s, %s, 'test-only', 'C-TEST', 'T-A', %s, 'member')",
                  (f"U-{n}", 100 + n, f"직원 {n}"))
    rubric(db, "RUB-quote", "v2", "quote")
    Masker = mcp("common.runtime").Masker
    return mcp("business_mcp.team").TeamStore(db, SimpleNamespace(masker=Masker(MASKING)))


def rubric(db, doc_id, version, work_type, items=ITEMS):
    db.conn.execute(
        "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, indexed, "
        "status, work_type) "
        "VALUES (%s, %s, 'C-TEST', '평가 기준', '사내', 'rubrics', true, '현행', %s)",
        (doc_id, version, work_type))
    for seq, item in enumerate(items, 1):
        db.conn.execute(
            "INSERT INTO rubric_items (doc_id, version, item_id, seq, item, criterion, score_2, "
            "score_0, evidence_sources, related_clauses) "
            "VALUES (%s, %s, %s, %s, %s, '기준', '2점', '0점', '{}', '{}')",
            (doc_id, version, f"{work_type}.{seq}", seq, item))


def work(db, work_id, employee, *, work_type="quote", team="T-A", completed="2026-10-10"):
    db.conn.execute(
        "INSERT INTO work_cases (work_id, company_id, team_id, employee_id, work_type, status, "
        "created_at, completed_at) VALUES (%s, 'C-TEST', %s, %s, %s, '완료', now(), %s)",
        (work_id, team, employee, work_type,
         datetime.fromisoformat(completed).replace(tzinfo=KST)))


def evaluation(db, eval_id, work_id, scores, *, status="확정", work_type="quote", version="v2",
               created="2026-10-11T00:00:00+09:00"):
    """scores: 항목 순서대로 점수 또는 '미관측'·'검토 필요'."""
    db.conn.execute(
        "INSERT INTO evaluations (evaluation_id, work_id, company_id, artifact_id, "
        "artifact_version, rubric_version, status, created_at) "
        "VALUES (%s, %s, 'C-TEST', 'ART', 'v1', %s, %s, %s)",
        (eval_id, work_id, version, status, created))
    items = []
    for seq, (item, score) in enumerate(zip(ITEMS, scores), 1):
        state = score if isinstance(score, str) else "채점"
        items.append({"item_id": f"{work_type}.{seq}", "item": item, "status": state,
                      "score": None if isinstance(score, str) else score, "reason": "",
                      "evidence_ids": []})
    db.conn.execute("INSERT INTO evaluation_drafts (evaluation_id, revision, items) "
                    "VALUES (%s, 1, %s)", (eval_id, Jsonb(items)))


def seed_october(db):
    """구성원 6명·업무 7건의 확정 평가. 결과 검증은 4명만 관측해 공개 기준 미달이다."""
    rows = {
        "W-1": ("U-1", [2, 2, 2, 2, 1]), "W-2": ("U-2", [2, 1, "미관측", 2, 1]),
        "W-3": ("U-3", [1, 1, 2, 2, 2]), "W-4": ("U-4", [2, 0, "검토 필요", 0, 1]),
        "W-5": ("U-5", [2, 1, 1, 2, 2]), "W-6": ("U-6", [1, 0, 2, 2, 1]),
        "W-7": ("U-1", [2, 1, 1, 2, 1]),
    }
    for work_id, (user, scores) in rows.items():
        work(db, work_id, user)
        evaluation(db, f"E-{work_id}", work_id, scores)
    # 같은 업무의 이전 확정 평가는 세지 않는다
    evaluation(db, "E-W-1-old", "W-1", [0, 0, 0, 0, 0], created="2026-10-09T00:00:00+09:00")
    # 확정 전·기간 밖·다른 팀은 세지 않는다
    work(db, "W-8", "U-7")
    evaluation(db, "E-W-8", "W-8", [0, 0, 0, 0, 0], status="확인 대기")
    work(db, "W-9", "U-7", completed="2026-11-01")
    evaluation(db, "E-W-9", "W-9", [0, 0, 0, 0, 0])
    work(db, "W-10", "U-7", team="T-B")
    evaluation(db, "E-W-10", "W-10", [0, 0, 0, 0, 0])


def claims_for(team, request_id):
    body = {"request_id": request_id}
    return team.run_claims(body)


def decide(snapshot):
    """Agent가 고를 수 있는 선택(각 band에서 첫 지표, 개선은 그 항목의 첫 활동)."""
    rows = metrics_for(snapshot)
    strengths = [m.metric_id for m in rows if m.band == "strength"][:1]
    improvements = [{"metric_id": m.metric_id,
                     "action_id": next(k for k, v in ACTIONS.items() if v[0] == m.criterion)}
                    for m in rows if m.band == "improvement"][:1]
    return TeamDecision.model_validate({"strengths": strengths, "improvements": improvements})


class Gateway:
    """MCP 봉투를 흉내 내 TeamStore를 직접 부른다(서버의 ToolRuntime과 같은 data·meta 모양)."""

    def __init__(self, team, claims):
        self.team, self.claims = team, claims

    async def call(self, server, tool, args):
        Result = mcp("common.runtime").Result
        fn = {"get_team_evaluation_snapshot": lambda: self.team.snapshot(self.claims, **args),
              "save_team_evaluation_report": lambda: self.team.save(self.claims, **args)}[tool]
        res = fn()
        meta = {"run_id": self.claims["run_id"], "snapshot": self.claims["snapshot"]}
        if isinstance(res, Result):
            meta, res = {**meta, **res.meta}, res.data
        return {"ok": True, "data": res, "meta": meta}


def fail_code(fn):
    MCPFail = mcp("common.runtime").MCPFail
    with pytest.raises(MCPFail) as error:
        fn()
    return error.value.code


def test_snapshot_aggregates_latest_final_and_matches_agent_contract(team, db):
    seed_october(db)
    row, created = team.create_request(OCT)
    assert created and row["status"] == "대기" and row["min_members"] == 5
    snapshot = TeamSnapshot.model_validate(row["snapshot"])
    assert content_hash(snapshot) == row["snapshot_sha256"]
    assert str(snapshot.team_id) == mcp("business_mcp.team").team_uuid("C-TEST", "T-A")
    [group] = snapshot.groups
    assert (group.work_type, group.rubric_version, group.members, group.finalized_evaluations) == (
        "quote", "v2", 6, 7)
    cells = {c.criterion: c.statistics for c in group.cells}
    assert cells["problem_definition"].model_dump() == {
        "scored_members": 6, "scored_evaluations": 7, "score_sum": 12}
    assert cells["instruction_quality"].model_dump() == {
        "scored_members": 6, "scored_evaluations": 7, "score_sum": 6}
    assert cells["verification"] is None  # 관측 구성원 4명
    assert cells["security"].model_dump() == {
        "scored_members": 6, "scored_evaluations": 7, "score_sum": 12}
    assert cells["output_quality"].score_sum == 9
    stored = db.conn.execute("SELECT source_evaluation_ids FROM team_evaluations").fetchone()
    assert sorted(stored["source_evaluation_ids"]) == [f"E-W-{n}" for n in range(1, 8)]
    # 같은 요청 ID·같은 범위는 기존 행, 다른 범위는 충돌
    again, created = team.create_request({**OCT, "request_id": row["request_id"]})
    assert not created and again["snapshot_id"] == row["snapshot_id"]
    assert fail_code(lambda: team.create_request(
        {**OCT, "request_id": row["request_id"], "release_approved": False})) == "VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_agent_workflow_saves_report_and_replays(team, db):
    seed_october(db)
    row, _ = team.create_request(OCT)
    claims = claims_for(team, row["request_id"])
    assert claims["kind"] == "team" and claims["team_id"] == "T-A"

    class Agent:
        async def generate(self, snapshot):
            return build_report(snapshot, decide(snapshot))

    workflow = TeamEvaluationWorkflow(Gateway(team, claims), Agent())
    snapshot, report = await workflow.prepare(UUID(row["request_id"]))
    assert report.status == "generated" and report.strengths and report.improvements
    receipt = await workflow.save(snapshot, report)
    assert isinstance(receipt, TeamSaveReceipt) and receipt.revision == 1
    detail = team.get(row["request_id"])
    assert detail["status"] == "완료" and detail["report"] == report.model_dump(mode="json")
    # 응답 유실 뒤 같은 내용 재전송은 처음 영수증
    again = Gateway(team, claims)
    request = build_save_request(snapshot, report).model_dump(mode="json")
    replay = await again.call("business", "save_team_evaluation_report", request)
    assert replay["meta"]["duplicate"] is True and replay["data"] == receipt.model_dump(mode="json")
    # 같은 요청에 다른 리포트는 충돌(재생성은 새 요청)
    first = decide(snapshot)
    criterion = {m.metric_id: m.criterion for m in metrics_for(snapshot)}
    other = build_report(snapshot, TeamDecision.model_validate({
        "strengths": first.strengths,
        "improvements": [{"metric_id": row.metric_id, "action_id": next(
            k for k, v in reversed(ACTIONS.items()) if v[0] == criterion[row.metric_id])}
            for row in first.improvements]}))
    assert other != report
    changed = build_save_request(snapshot, report).model_copy(update={
        "report": other, "report_sha256": content_hash(other)}).model_dump(mode="json")
    assert fail_code(lambda: team.save(claims, **changed)) == "VERSION_CONFLICT"


def test_save_rejects_tampered_report_and_scope(team, db):
    seed_october(db)
    row, _ = team.create_request(OCT)
    claims = claims_for(team, row["request_id"])
    snapshot = TeamSnapshot.model_validate(row["snapshot"])
    report = build_report(snapshot, decide(snapshot))
    request = build_save_request(snapshot, report).model_dump(mode="json")
    digest = mcp("business_mcp.team").digest

    def save_with(change=None, owner=claims, **fields):
        body = {**request, **fields}
        if change:
            body["report"] = {**request["report"], **change}
            body["report_sha256"] = digest(body["report"])
        return fail_code(lambda: team.save(owner, **body))

    metrics = [dict(m) for m in request["report"]["metrics"]]
    metrics[0]["score_sum"] += 1
    strength = request["report"]["strengths"][0]["metric_id"]
    swapped = [{**request["report"]["improvements"][0], "metric_id": strength}]
    assert save_with({"metrics": metrics}) == "VALIDATION_ERROR"
    assert save_with({"summary": "담당 010-1234-5678"}) == "VALIDATION_ERROR"
    assert save_with({"improvements": swapped}) == "VALIDATION_ERROR"
    assert save_with(report_sha256="0" * 64) == "VALIDATION_ERROR"
    assert save_with(snapshot_id=str(uuid4())) == "VERSION_CONFLICT"
    assert save_with(owner={**claims, "company_id": "C-OTHER"}) == "FORBIDDEN"
    other_request = {**claims, "request_id": str(uuid4())}
    assert fail_code(lambda: team.snapshot(other_request, row["request_id"])) == "FORBIDDEN"
    assert team.get(row["request_id"])["status"] == "대기"


@pytest.mark.parametrize("change,status", [
    ({"release_approved": False}, "withheld"),
    ({"min_members": 7}, "insufficient_data"),
])
def test_withheld_or_small_groups_have_no_aggregate(team, db, change, status):
    seed_october(db)
    row, _ = team.create_request({**OCT, **change})
    snapshot = TeamSnapshot.model_validate(row["snapshot"])
    assert snapshot.groups == []
    report = build_report(snapshot, TeamDecision(strengths=[], improvements=[]))
    assert report.status == status
    request = build_save_request(snapshot, report).model_dump(mode="json")
    receipt = team.save(claims_for(team, row["request_id"]), **request)
    assert TeamSaveReceipt.model_validate(receipt).status == "saved"


def test_rejects_unmapped_rubric_and_bad_request(team, db):
    rubric(db, "RUB-proposal", "v2", "proposal", items=[*ITEMS[:4], "고객 만족"])
    work(db, "W-P", "U-1", work_type="proposal")
    evaluation(db, "E-W-P", "W-P", [2, 2, 2, 2, 2], work_type="proposal")
    def create(**change):
        return fail_code(lambda: team.create_request({**OCT, **change}))

    assert create() == "VALIDATION_ERROR"
    assert create(release_approved="yes") == "VALIDATION_ERROR"
    assert create(period_end="2026-10-01") == "VALIDATION_ERROR"
    assert create(team_id="T-X") == "NOT_FOUND"
    assert fail_code(lambda: team.run_claims({"request_id": str(uuid4())})) == "NOT_FOUND"
