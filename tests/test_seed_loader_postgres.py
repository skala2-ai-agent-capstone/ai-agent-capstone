"""시드 적재기(mcp_servers/db/loader/load_postgres.py)를 다시 돌려도 앱이 쓴 문서가 남는지 본다.

db-init은 `docker compose up` 때마다 다시 돌므로, 관리자가 올린 내규와 평가 기준 초안·승인본이
시드로 되돌아가면 안 된다. FEEDBACK_TEST_DATABASE_URL이 지정된 테스트 DB 안에 임의 스키마를 만들고
종료 시 삭제한다. psycopg와 mcp_servers를 PYTHONPATH에 준비해야 한다.
"""

import json
import os
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("FEEDBACK_TEST_DATABASE_URL"), reason="테스트용 PostgreSQL 미지정"
)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "mcp_servers/data"
SCHEMAS = sorted((ROOT / "mcp_servers/db/postgres").glob("00*.sql"))
SEED_DOCS = json.loads((DATA / "seed/documents.json").read_text(encoding="utf-8"))
COMPANY = "C-LGCNS-SALES"
ITEM_COLS = ("item_id, seq, item, criterion, score_2, score_1, score_0, unobserved, needs_review, "
             "evidence_sources, related_clauses")
# aworko-server Flyway V1·V4~V6이 같은 DB에 만드는 테이블(flyway_backend_history 제외)
BACKEND_TABLES = ["outbox_events", "evaluation_failures", "aggregation_approvals",
                  "rubric_generations", "evaluation_result_receipts", "team_evaluation_failures"]


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("SEED_USER_PASSWORD", "test-only")
    schema = "loader_test_" + uuid4().hex
    url = os.environ["FEEDBACK_TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(make_conninfo(url, options=f"-c search_path={schema}")) as c:
                for path in SCHEMAS:
                    c.execute(path.read_text(encoding="utf-8"))
                c.commit()
                yield c
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def load(conn, reset=False):
    loader = import_module("db.loader.load_postgres")
    with conn.transaction():
        return loader.load(conn, str(DATA), reset)


def count(conn, query, *params):
    return conn.execute(query, params).fetchone()[0]


def statuses(conn, *doc_ids):
    rows = conn.execute("SELECT doc_id, version, status FROM documents WHERE doc_id = ANY(%s)",
                        (list(doc_ids),)).fetchall()
    return {(d, v): s for d, v, s in rows}


def app_writes(conn):
    """business-mcp의 쓰기를 같은 SQL로 흉내 낸다(documents.py 내규 업로드, rubrics.py 승인)."""
    with conn.transaction():
        # 관리자 내규 업로드: 새 버전을 현행으로 넣고 회사의 이전 현행 내규를 폐기
        conn.execute(
            "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, "
            "indexed, status, effective_from, content, source_file, note) "
            "VALUES ('SALES-AI-GL-001', 'v1.1', %s, '내규', '사내', 'policies', true, '현행', "
            "current_date, '제1조 본문', 'policy.pdf', '관리자 업로드')", (COMPANY,))
        conn.execute("INSERT INTO document_chunks VALUES "
                     "('SALES-AI-GL-001', 'v1.1', 'c1', 1, '제1조', '제1조', '제1조 본문')")
        conn.execute(
            "UPDATE documents SET status='폐기' WHERE company_id=%s AND collection='policies' "
            "AND status='현행' AND NOT (doc_id='SALES-AI-GL-001' AND version='v1.1')", (COMPANY,))
        # 견적서 기준 v3 승인(시드 v2 폐기)과 제안서 기준 v3 초안
        for work_type, status in (("quote", "현행"), ("proposal", "초안")):
            doc_id = f"RUB-{work_type}"
            conn.execute(
                "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, "
                "indexed, status, work_type, note) VALUES (%s, 'v3', %s, '평가 기준표', '사내', "
                "'rubrics', true, %s, %s, '평가 기준 생성 Agent')",
                (doc_id, COMPANY, status, work_type))
            conn.execute(
                f"INSERT INTO rubric_items (doc_id, version, {ITEM_COLS}) "
                f"SELECT doc_id, 'v3', {ITEM_COLS} FROM rubric_items "
                "WHERE doc_id=%s AND version='v2'", (doc_id,))
        conn.execute("UPDATE documents SET status='폐기' WHERE doc_id='RUB-quote' AND version='v2'")


def test_rerun_keeps_app_documents_and_their_retirements(conn):
    load(conn)
    app_writes(conn)

    load(conn)

    assert statuses(conn, "SALES-AI-GL-001", "RUB-quote", "RUB-proposal") == {
        ("SALES-AI-GL-001", "v1.0"): "폐기", ("SALES-AI-GL-001", "v1.1"): "현행",
        ("RUB-quote", "v2"): "폐기", ("RUB-quote", "v3"): "현행",
        ("RUB-proposal", "v2"): "현행", ("RUB-proposal", "v3"): "초안",
    }
    items = conn.execute("SELECT doc_id, count(*) FROM rubric_items WHERE version='v3' "
                         "GROUP BY doc_id").fetchall()
    assert dict(items) == {"RUB-quote": 5, "RUB-proposal": 5}
    assert count(conn, "SELECT count(*) FROM document_chunks "
                       "WHERE doc_id='SALES-AI-GL-001' AND version='v1.1'") == 1
    # 폐기를 되살리지 않으므로 현행 내규와 유형별 현행 기준은 하나씩이다
    assert count(conn, "SELECT count(*) FROM documents "
                       "WHERE collection='policies' AND status='현행'") == 1
    assert conn.execute("SELECT work_type FROM documents WHERE collection='rubrics' "
                        "AND status='현행' GROUP BY work_type HAVING count(*) > 1").fetchall() == []


def test_rerun_still_restores_seed_documents(conn):
    load(conn)
    with conn.transaction():
        conn.execute("UPDATE documents SET title='바뀐 제목', status='초안' "
                     "WHERE doc_id='PROD-DOCAI'")
        conn.execute("DELETE FROM document_chunks WHERE doc_id='PROD-DOCAI'")

    load(conn)

    seed = next(d for d in SEED_DOCS if d["doc_id"] == "PROD-DOCAI")
    assert conn.execute("SELECT title, status FROM documents WHERE doc_id='PROD-DOCAI'"
                        ).fetchone() == (seed["title"], seed["status"])
    assert count(conn, "SELECT count(*) FROM document_chunks WHERE doc_id='PROD-DOCAI'"
                 ) == len(seed["chunks"])
    assert count(conn, "SELECT count(*) FROM documents") == len(SEED_DOCS)


def test_reset_clears_app_documents_and_backend_tables(conn):
    with conn.transaction():
        # 백엔드 테이블의 대역(FK 없음). --reset이 이름으로 비우는지만 본다
        for table in BACKEND_TABLES:
            conn.execute(sql.SQL("CREATE TABLE {} (id text)").format(sql.Identifier(table)))
            conn.execute(sql.SQL("INSERT INTO {} VALUES ('old')").format(sql.Identifier(table)))
    load(conn)
    app_writes(conn)

    out = load(conn, reset=True)

    assert sorted(out["backend_cleared"]) == sorted(BACKEND_TABLES)
    for table in BACKEND_TABLES:
        assert count(conn, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))) == 0
    assert count(conn, "SELECT count(*) FROM documents") == len(SEED_DOCS)
    assert statuses(conn, "RUB-quote") == {("RUB-quote", "v2"): "현행"}
