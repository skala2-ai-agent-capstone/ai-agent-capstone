"""최종 평가 저장: 실제 MCP SDK/HTTP + PostgreSQL WriteStore, 모델·자료 조회는 대역.

FEEDBACK_TEST_DATABASE_URL이 지정된 테스트 DB에만 임의 스키마를 생성한다.
"""

import json
import os
from importlib import import_module

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from test_stored_final_evaluation import ROOT, StoredGateway, client_fixture

from ai_agents.integrations.final_mcp import FinalMCPGateway
from ai_agents.orchestration.final_evaluation import FinalEvaluationJob, FinalEvaluationWorkflow

psycopg = pytest.importorskip("psycopg")
db = import_module("test_feedback_postgres").db

pytestmark = pytest.mark.skipif(
    not os.getenv("FEEDBACK_TEST_DATABASE_URL"), reason="테스트용 PostgreSQL 미지정"
)


@pytest.fixture
def storage(db, monkeypatch):
    loader = import_module("db.loader.load_postgres")
    monkeypatch.setenv("SEED_USER_PASSWORD", "isolated-test-only")
    old_factory = db.conn.row_factory
    try:
        db.conn.row_factory = psycopg.rows.tuple_row
        loader.load(db.conn, str(ROOT / "mcp_servers/data"), reset=False)
    finally:
        db.conn.row_factory = old_factory
    Masker = import_module("common.runtime").Masker
    policy = json.loads((ROOT / "mcp_servers/data/seed/policy_rules.json").read_text())
    repo = import_module("business_mcp.pg").Repo(db)
    store = import_module("business_mcp.store").Store(repo, Masker(policy))
    writes = import_module("business_mcp.writes").WriteStore(db, store)
    data = StoredGateway("rfp_analysis")
    wid = data.fixture.context.work_id
    target = next(
        e
        for e in writes.list_evaluations(wid)
        if e["artifact_version"] == data.fixture.context.target.version
    )
    claims = {
        "company_id": target["company_id"],
        "evaluation_id": target["evaluation_id"],
        "work_id": wid,
        "run_id": data.fixture.context.run_id,
        "snapshot": store.build_snapshot(wid, target["artifact_version"]),
    }
    assert claims["snapshot"] == data.fixture.context.snapshot
    return writes, store, data, claims


@pytest.mark.asyncio
async def test_final_sdk_saves_real_draft_and_coaching_once_per_run(
    db, storage, tmp_path, monkeypatch,
):
    writes, store, data, claims = storage
    server = MCPServer("final-storage-test")

    def envelope(value):
        return {
            "ok": True,
            "data": value,
            "meta": {"run_id": claims["run_id"], "snapshot": claims["snapshot"]},
        }

    @server.tool()
    async def get_evaluation_context(evaluation_id: str) -> dict:
        return envelope(writes.evaluation_context(claims, evaluation_id))

    @server.tool()
    async def search_rubric(work_type: str, rubric_version: str) -> dict:
        rubric = store.rubric(work_type, rubric_version)
        return envelope(
            {
                **{k: rubric[k] for k in ("doc_id", "version", "status", "work_type", "items")},
                "content_type": "data",
            }
        )

    @server.tool()
    async def save_evaluation_draft(
        evaluation_id: str, rubric_version: str, items: list[dict], idempotency_key: str
    ) -> dict:
        saved = writes.save_evaluation_draft(
            claims, evaluation_id, rubric_version, items, idempotency_key
        )
        return envelope(saved.data if hasattr(saved, "data") else saved)

    @server.tool()
    async def save_coaching(evaluation_id: str, coaching: list[dict], idempotency_key: str) -> dict:
        saved = writes.save_coaching(claims, evaluation_id, coaching, idempotency_key)
        return envelope(saved.data if hasattr(saved, "data") else saved)

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    headers_seen = []

    def http_client(headers):
        headers_seen.append(headers)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)

    monkeypatch.setattr("ai_agents.integrations.mcp_client.create_mcp_http_client", http_client)

    class Gateway(FinalMCPGateway):
        async def call(self, server, tool, arguments):
            if tool in {
                "get_evaluation_context",
                "search_rubric",
                "save_evaluation_draft",
                "save_coaching",
            }:
                return await super().call(server, tool, arguments)
            return await data.call(server, tool, arguments)

    client = client_fixture()
    job = FinalEvaluationJob(evaluation_id=claims["evaluation_id"], operation_id="test-db")
    async with app.router.lifespan_context(app):
        async with Gateway(
            "test-secret", business_url="http://testserver/mcp", rag_url="http://testserver/mcp"
        ) as gateway:
            workflow = FinalEvaluationWorkflow(
                gateway, client, model="mock-model", checkpoint_dir=tmp_path, api_mode="responses"
            )
            result = await workflow.run(job)
            assert await workflow.run(job) == result
            client.responses.parse.assert_awaited_once()
            detail = writes.detail(job.evaluation_id)
            assert detail["draft"]["revision"] == detail["coaching"]["revision"] == 1
            # 팀장 재처리(새 message_id → 새 실행): 이전 판이 있어도 다시 평가해 새 판을 저장한다
            retry = FinalEvaluationJob(evaluation_id=job.evaluation_id,
                                       operation_id="test-db-retry")
            again = await workflow.run(retry)
    assert client.responses.parse.await_count == 2
    assert again["receipts"]["save_evaluation_draft"]["draft_revision"] == 2
    assert again["receipts"]["save_coaching"]["coaching_revision"] == 2
    assert all(h == {"Authorization": "Bearer test-secret"} for h in headers_seen)
    detail = writes.detail(job.evaluation_id)
    assert detail["draft"]["revision"] == detail["coaching"]["revision"] == 2
    assert len(detail["draft"]["items"]) == len(detail["coaching"]["items"]) == 5
    assert detail["status"] == "대기"  # Agent는 서버의 확정·확인 대기 상태를 만들지 않는다.
    keys = db.q("SELECT tool FROM idempotency_keys WHERE evaluation_id=%s", (job.evaluation_id,))
    assert sorted(r["tool"] for r in keys) == ["save_coaching", "save_coaching",
                                               "save_evaluation_draft", "save_evaluation_draft"]


def test_context_rejects_another_evaluation_and_company(storage):
    writes, _, _, claims = storage
    MCPFail = import_module("common.runtime").MCPFail
    with pytest.raises(MCPFail):
        writes.evaluation_context(claims, "ANOTHER-EVALUATION")
    with pytest.raises(MCPFail):
        writes.evaluation_context(
            {**claims, "company_id": "ANOTHER-COMPANY"}, claims["evaluation_id"]
        )
