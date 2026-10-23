"""중간 피드백의 실제 PostgreSQL 조회·스냅샷·Agent 계약 검사.

FEEDBACK_TEST_DATABASE_URL이 지정된 테스트 DB 안에 임의 스키마를 만들고 종료 시 삭제한다.
운영 DB를 지정하지 않는다. psycopg와 mcp_servers를 PYTHONPATH에 준비해야 한다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_agents.evaluation_agent.feedback.agent import FeedbackAgent
from ai_agents.evaluation_agent.feedback.validation import validate_messages
from ai_agents.shared.live_feedback_contracts import LiveEvaluationContext, MessageBatch
from ai_agents.validation.feedback_targets import FixtureChatClient

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("FEEDBACK_TEST_DATABASE_URL"), reason="테스트용 PostgreSQL 미지정"
)
ROOT = Path(__file__).resolve().parents[1]
CLAIMS = {"company_id": "C-TEST"}
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    schema = "feedback_test_" + uuid4().hex
    url = os.environ["FEEDBACK_TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        database = None
        try:
            Db = import_module("business_mcp.pg").Db
            database = Db(make_conninfo(url, options=f"-c search_path={schema}"))
            database.conn.execute((ROOT / "mcp_servers/db/postgres/001_schema.sql").read_text())
            database.conn.execute("INSERT INTO companies VALUES ('C-TEST', '테스트 회사', 5)")
            database.conn.execute(
                "INSERT INTO users (user_id, employee_no, password, company_id, name, role) "
                "VALUES ('U-TEST', 1, 'test-only', 'C-TEST', '테스트 직원', 'member')"
            )
            database.conn.execute(
                "INSERT INTO customers (customer_id, company_id, name) "
                "VALUES ('CUSTOMER', 'C-TEST', '테스트 고객')"
            )
            for work in ("W-TEST", "W-OTHER"):
                database.conn.execute(
                    "INSERT INTO work_cases (work_id, company_id, employee_id, customer_id, "
                    "status, created_at) "
                    "VALUES (%s, 'C-TEST', 'U-TEST', 'CUSTOMER', '진행 중', now())",
                    (work,),
                )
            yield database
        finally:
            if database:
                database.conn.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def live(db):
    Masker = import_module("common.runtime").Masker
    return import_module("business_mcp.live").LiveStore(
        db, SimpleNamespace(masking_version="mask-test", masker=Masker({"rules": []}))
    )


def exchange(db, n, *, question=None, answer=None, error=None, status=200,
             stage="workspace_chat", work="W-TEST", at=None, history=(), stream=False):
    question = question if question is not None else f"질문 {n}"
    answer = answer if answer is not None else f"답변 {n}"
    messages = [*history, {"role": "user", "content": question}]
    response = {"choices": [{"message": {"role": "assistant", "content": answer}}]}
    sse = (
        "data: " + json.dumps({"choices": [{"delta": {"content": answer}}]})
        + "\n\ndata: [DONE]\n"
    )
    return db.conn.execute(
        "INSERT INTO llm_exchanges (work_id, user_id, stage, created_at, status_code, error, "
        "request, response, response_text, stream, latency_ms) "
        "VALUES (%s, 'U-TEST', %s, %s, %s, %s, %s, %s, %s, %s, 10) RETURNING id",
        (work, stage, at or BASE + timedelta(seconds=n), status, error,
         Jsonb({"messages": messages}), None if stream else Jsonb(response),
         sse if stream else None, stream),
    ).fetchone()["id"]


def request_and_read(live):
    row, created = live.create_request({"work_id": "W-TEST", "requested_by": "U-TEST"})
    assert created and "conversation_snapshot" not in row
    context = live.start(CLAIMS, row["live_eval_id"])
    batch = live.get_messages(
        CLAIMS, row["live_eval_id"], "W-TEST", row["from_seq"], row["to_seq"]
    )
    validate_messages(
        LiveEvaluationContext.model_validate(context), MessageBatch.model_validate(batch)
    )
    return row, context, batch


@pytest.mark.parametrize("count", [1, 4, 5, 7])
def test_latest_five_completed_calls_not_five_message_rows(db, live, count):
    history, ids = [], []
    for n in range(count):
        ids.append(exchange(db, n, history=history, stream=(n % 2 == 0)))
        history.extend([{"role": "user", "content": f"질문 {n}"},
                        {"role": "assistant", "content": f"답변 {n}"}])
    _, _, batch = request_and_read(live)
    rows = batch["messages"]
    assert len(rows) == 2 * min(count, 5)
    assert [m["exchange_id"] for m in rows[::2]] == ids[-5:]
    assert [m["role"] for m in rows] == ["user", "assistant"] * min(count, 5)
    assert [m["content"] for m in rows[::2]] == [f"질문 {n}" for n in range(max(0, count-5), count)]
    assert all(q["exchange_id"] == a["exchange_id"] for q, a in zip(rows[::2], rows[1::2]))


def test_excludes_failed_future_empty_other_work_and_agent_calls(db, live):
    wanted = [exchange(db, n) for n in range(5)]
    exchange(db, 10, error="upstream_stream_broken", stream=True)
    exchange(db, 11, status=500)  # 오류 문자열 누락 시에도 실패 HTTP 응답 제외
    exchange(db, 12, answer="   ")
    exchange(db, 13, stage="live_evaluate")
    exchange(db, 14, work="W-OTHER")
    exchange(db, 15, at=datetime.now(timezone.utc) + timedelta(days=1))
    _, _, batch = request_and_read(live)
    assert [m["exchange_id"] for m in batch["messages"][::2]] == wanted


def test_zero_completed_pairs_does_not_create_request(db, live):
    exchange(db, 1, error="upstream_timeout")
    with pytest.raises(import_module("common.runtime").MCPFail):
        live.create_request({"work_id": "W-TEST", "requested_by": "U-TEST"})
    assert db.one("SELECT count(*) AS n FROM live_evaluations")["n"] == 0


def test_failed_question_retry_pairs_once_with_successful_exchange(db, live):
    exchange(db, 1, question="동일 질문", error="upstream_timeout")
    succeeded = exchange(db, 2, question="동일 질문", answer="재시도 답변")
    _, _, batch = request_and_read(live)
    assert [m["content"] for m in batch["messages"]] == ["동일 질문", "재시도 답변"]
    assert [m["exchange_id"] for m in batch["messages"]] == [succeeded, succeeded]


def test_request_cutoff_excludes_call_finishing_during_request(db, live, monkeypatch):
    first = exchange(db, 1)
    select = live._recent_messages

    def finish_after_request(c, work_id, requested_at):
        exchange(db, 2, at=requested_at + timedelta(microseconds=1))
        return select(c, work_id, requested_at)

    monkeypatch.setattr(live, "_recent_messages", finish_after_request)
    _, _, batch = request_and_read(live)
    assert [m["exchange_id"] for m in batch["messages"]] == [first, first]


@pytest.mark.asyncio
async def test_snapshot_survives_new_calls_edits_and_view_renumbering_through_save(db, live):
    ids = [exchange(db, n) for n in range(7)]
    row, context, before = request_and_read(live)
    exchange(db, 8)
    # 마스킹 규칙·원본 변경 및 과거 행 삭제로 뷰 seq가 바뀌어도 당시 근거를 유지한다.
    live.store.masking_version = "mask-new"
    db.conn.execute("DELETE FROM llm_exchanges WHERE id=%s", (ids[0],))
    db.conn.execute("UPDATE llm_exchanges SET response='{}' WHERE id=%s", (ids[-1],))
    after = live.get_messages(CLAIMS, row["live_eval_id"], "W-TEST", row["from_seq"], row["to_seq"])
    assert after == before
    duplicate, created = live.create_request(
        {"work_id": "W-TEST", "requested_by": "U-TEST", "live_eval_id": row["live_eval_id"]}
    )
    assert not created and duplicate["from_seq"] == row["from_seq"]
    q = before["messages"][0]
    client = FixtureChatClient({
        "items": [{"item_id": "F-1", "criterion_id": None, "kind": "improvement",
                   "diagnosis": "출력 형식을 지정하세요.",
                   "evidence": [{"message_id": q["message_id"], "quote": q["content"]}]}],
        "coaching": [{"item_id": "F-1", "target_message_id": q["message_id"],
                      "next_action": "표 형식을 요청하세요.",
                      "suggested_prompt": "표로 정리해줘."}],
    })
    feedback = await FeedbackAgent(client, "test-model").generate(
        LiveEvaluationContext.model_validate(context), MessageBatch.model_validate(after)
    )
    result = feedback.model_dump(mode="json")
    saved = live.save(
        CLAIMS, row["live_eval_id"], "완료", result["items"],
        {"schema_version": "1", "provenance": {}, "suggestions": result["coaching"]},
        "test-model", 1, None,
    )
    assert saved["status"] == "완료" and not saved["duplicate"]


def test_snapshot_uses_masked_text(db, live):
    db.conn.execute(
        "INSERT INTO policy_rules (rule_id, seq, kind, pattern, replace_with) "
        "VALUES ('MASK-TEST', 1, 'masking', 'PRIVATE', '[마스킹]')"
    )
    exchange(db, 1, question="PRIVATE 질문", answer="PRIVATE 답변")
    row, _, batch = request_and_read(live)
    stored = db.one("SELECT conversation_snapshot FROM live_evaluations WHERE live_eval_id=%s",
                    (row["live_eval_id"],))["conversation_snapshot"]
    assert "PRIVATE" not in json.dumps(stored)
    assert [m["content"] for m in batch["messages"]] == ["[마스킹] 질문", "[마스킹] 답변"]


def test_snapshot_migration_is_additive_and_old_pending_request_fails_closed(db, live):
    exchange(db, 1)
    row, _, _ = request_and_read(live)
    db.conn.execute("ALTER TABLE live_evaluations DROP COLUMN conversation_snapshot")
    migration = (ROOT / "mcp_servers/db/postgres/002_live_feedback_snapshot.sql").read_text()
    db.conn.execute(migration)
    db.conn.execute(migration)
    assert live.get(row["live_eval_id"])["status"] == "생성 중"
    with pytest.raises(import_module("common.runtime").MCPFail, match="새로 요청"):
        live.get_messages(CLAIMS, row["live_eval_id"], "W-TEST", row["from_seq"], row["to_seq"])


def test_latest_is_completion_time_with_id_tiebreaker(db, live):
    ids = [exchange(db, n, at=BASE + timedelta(seconds=10)) for n in range(6)]
    exchange(db, 7, at=BASE)  # 늦게 적재된 과거 응답은 최신 대화가 아니다.
    _, _, batch = request_and_read(live)
    assert [m["exchange_id"] for m in batch["messages"][::2]] == ids[-5:]


def test_does_not_pair_unanswered_question_with_unrelated_orphan_answer(db, live):
    first = exchange(db, 1)
    exchange(db, 2, question="답 없는 질문", error="upstream_timeout")
    orphan = exchange(db, 3, answer="독립적인 응답")
    db.conn.execute("UPDATE llm_exchanges SET request='{}' WHERE id=%s", (orphan,))
    _, _, batch = request_and_read(live)
    assert [m["exchange_id"] for m in batch["messages"]] == [first, first]
