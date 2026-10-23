import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError
from pydantic import ValidationError
from test_criteria_mcp_input import policy_reply
from test_rubric_builder import fake_rubric, response

from ai_agents.cli import rubric_worker as cli
from ai_agents.criteria_agent.builder import OpenAIRubricBuilder, RubricError, canonical_work_type
from ai_agents.integrations.mcp_client import MCPError
from ai_agents.integrations.rubric_mcp import RubricMCPGateway, token_claims
from ai_agents.orchestration.rubric_worker import (
    DLQ,
    RESULTS_QUEUE,
    RETRY_QUEUE,
    RubricGenerationError,
    RubricWorker,
    classify,
    generate_drafts,
)
from ai_agents.shared.rubric_generation_contracts import (
    RubricGenerationEvent,
    RubricGenerationResult,
    SavedDraft,
)

COMPANY = "C-LGCNS-SALES"


def event(**overrides):
    data = {
        "message_id": "6c1f4a2e-0000-4000-8000-000000000001",
        "type": "RUBRIC_GENERATE",
        "generation_id": "RG-3f2a9c1e",
        "company_id": COMPANY,
        "policy_doc_id": "test-policy",
        "policy_version": "test-v1",
        "work_types": ["rfp_analysis", "proposal", "quote", "inquiry_reply"],
        "requested_by": "L-3001",
        "attempt": 0,
    }
    return {**data, **overrides}


def body(**overrides):
    return json.dumps(event(**overrides)).encode()


def worker(execute, tmp_path, **kwargs):
    return RubricWorker(execute, company_id=COMPANY, trace_dir=tmp_path, model="test",
                        code_provenance={}, **kwargs)


class FakeMCP:
    """search_policy는 요청한 조항으로 내규를, save_rubric은 유형별 초안 버전을 돌려준다."""

    def __init__(self, doc_id="test-policy", version="test-v1"):
        self.saved, self.doc_id, self.version = [], doc_id, version

    async def call(self, server, tool, arguments):
        if tool == "search_policy":
            reply = policy_reply()
            reply["data"]["chunks"] = [{
                "ref": f"{self.doc_id}@{self.version}#{c}-1", "doc_id": self.doc_id,
                "version": self.version, "chunk_id": f"{c}-1", "clause_id": c, "label": c,
                "text": f"① {c}의 가상 지침이다.\n구간 원문을 보존한다.",
                "content_type": "data", "score": None,
            } for c in arguments["clause_ids"]]
            return reply
        assert (server, tool) == ("business", "save_rubric")
        self.saved.append(arguments)
        return {"ok": True, "data": {"doc_id": f"RUB-{arguments['work_type']}", "version": "v3",
                                     "work_type": arguments["work_type"], "status": "초안",
                                     "replaced_draft": len(self.saved) == 1},
                "meta": {"run_id": "RUN-1", "snapshot": {}}}


def builder():
    """요청 본문의 업무 유형에 맞는 가짜 기준표를 돌려주는 모델."""
    async def parse(**kwargs):
        work_type = json.loads(kwargs["input"][1]["content"])["work_type"]
        return response(fake_rubric(canonical_work_type(work_type)))

    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(side_effect=parse)))
    return OpenAIRubricBuilder(client, "test-model", max_retries=0)


def test_event_contract_matches_backend_fields_and_rejects_extras():
    parsed = RubricGenerationEvent.model_validate_json(body())
    assert parsed.work_types == ["rfp_analysis", "proposal", "quote", "inquiry_reply"]
    with pytest.raises(ValidationError):
        RubricGenerationEvent.model_validate_json(body(secret="x"))
    with pytest.raises(ValidationError):
        RubricGenerationEvent.model_validate_json(body(work_types=["quote", "quote"]))
    with pytest.raises(ValidationError):
        RubricGenerationEvent.model_validate_json(body(work_types=["quotation"]))


@pytest.mark.asyncio
async def test_generate_drafts_saves_every_type_against_the_pinned_policy():
    mcp = FakeMCP()

    drafts = await generate_drafts(mcp, builder(), RubricGenerationEvent.model_validate(event()))

    all_types = ["rfp_analysis", "proposal", "quote", "inquiry_reply"]
    assert [d.work_type for d in drafts] == all_types
    assert drafts[2] == SavedDraft(work_type="quote", doc_id="RUB-quote", version="v3",
                                   replaced_draft=False)
    assert [s["work_type"] for s in mcp.saved] == all_types
    first = mcp.saved[0]
    assert len(first["items"]) == 5
    assert first["note"] == "내규 test-policy@test-v1 업로드로 자동 생성(RG-3f2a9c1e)"
    assert first["idempotency_key"].startswith("rubric-gen:RG-3f2a9c1e:rfp_analysis:")
    # 같은 내용을 다시 저장하면 같은 키(재전달 때 MCP가 이전 결과를 돌려준다)
    again = FakeMCP()
    await generate_drafts(again, builder(), RubricGenerationEvent.model_validate(event()))
    assert [s["idempotency_key"] for s in again.saved] == [s["idempotency_key"] for s in mcp.saved]


@pytest.mark.asyncio
async def test_search_returning_the_previous_policy_version_is_a_retryable_conflict():
    mcp = FakeMCP(version="test-v0")   # rag-mcp가 아직 새 버전을 읽지 않음

    with pytest.raises(MCPError) as caught:
        await generate_drafts(mcp, builder(), RubricGenerationEvent.model_validate(event()))

    assert caught.value.code == "VERSION_CONFLICT"
    assert classify(caught.value).retryable
    assert mcp.saved == []


@pytest.mark.asyncio
async def test_success_publishes_the_result_before_ack(tmp_path):
    order = []
    drafts = [SavedDraft(work_type="quote", doc_id="RUB-quote", version="v3", replaced_draft=True)]

    async def publish(queue, payload, code):
        order.append(("publish", queue, code))
        RubricGenerationResult.model_validate(payload)
        assert payload["generation_id"] == "RG-3f2a9c1e"
        assert payload["company_id"] == COMPANY
        assert payload["drafts"][0] == {"work_type": "quote", "doc_id": "RUB-quote",
                                        "version": "v3", "replaced_draft": True}

    async def ack():
        order.append(("ack",))

    result = await worker(AsyncMock(return_value=drafts), tmp_path).handle(
        body(), publish=publish, ack=ack)

    assert result["status"] == "완료"
    assert order == [("publish", RESULTS_QUEUE, None), ("ack",)]


@pytest.mark.asyncio
async def test_other_company_request_is_dead_lettered_without_generation(tmp_path):
    execute, publish, ack = AsyncMock(), AsyncMock(), AsyncMock()

    result = await worker(execute, tmp_path).handle(
        body(company_id="C-OTHER"), publish=publish, ack=ack)

    assert result == {"status": "dlq", "code": "TOKEN_SCOPE_MISMATCH",
                      "generation_id": "RG-3f2a9c1e", "trace_id": result["trace_id"]}
    execute.assert_not_awaited()
    assert publish.call_args.args[0] == DLQ
    assert publish.call_args.args[1]["company_id"] == "C-OTHER"   # 원래 요청 형식 그대로
    ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_retryable_failures_retry_with_next_attempt_then_dead_letter(tmp_path):
    publish, ack = AsyncMock(), AsyncMock()
    failing = AsyncMock(side_effect=RubricError("검증 실패"))

    first = await worker(failing, tmp_path, max_retries=2).handle(body(), publish=publish, ack=ack)
    assert first["status"] == "retry" and first["code"] == "RUBRIC_INVALID"
    queue, payload, code = publish.call_args.args
    assert (queue, payload["attempt"], code) == (RETRY_QUEUE, 1, "RUBRIC_INVALID")
    assert RubricGenerationEvent.model_validate(payload).type == "RUBRIC_GENERATE"

    last = await worker(failing, tmp_path, max_retries=2).handle(
        body(attempt=2), publish=publish, ack=ack)
    assert last["status"] == "dlq"
    assert publish.call_args.args[0] == DLQ and publish.call_args.args[1]["attempt"] == 2
    assert ack.await_count == 2


@pytest.mark.asyncio
async def test_policy_without_required_clauses_is_not_retried(tmp_path):
    publish = AsyncMock()
    failing = AsyncMock(side_effect=MCPError("POLICY_INCOMPLETE", "search_policy"))

    result = await worker(failing, tmp_path).handle(body(), publish=publish, ack=AsyncMock())

    assert result["status"] == "dlq" and result["code"] == "POLICY_INCOMPLETE"
    assert publish.call_args.args[2] == "POLICY_INCOMPLETE"


@pytest.mark.asyncio
async def test_malformed_event_keeps_only_the_generation_id(tmp_path):
    publish, ack = AsyncMock(), AsyncMock()
    execute = AsyncMock()

    await worker(execute, tmp_path).handle(body(secret="private"), publish=publish, ack=ack)

    queue, payload, code = publish.call_args.args
    assert (queue, code) == (DLQ, "INVALID_EVENT")
    assert payload["generation_id"] == "RG-3f2a9c1e" and "private" not in json.dumps(payload)
    await worker(execute, tmp_path).handle(b"not json", publish=publish, ack=ack)
    assert "generation_id" not in publish.call_args.args[1]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_ack(tmp_path):
    ack = AsyncMock()
    publish = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    drafts = [SavedDraft(work_type="quote", doc_id="RUB-quote", version="v3", replaced_draft=False)]

    with pytest.raises(RuntimeError):
        await worker(AsyncMock(return_value=drafts), tmp_path).handle(
            body(), publish=publish, ack=ack)

    ack.assert_not_awaited()


def test_error_classification():
    request = httpx.Request("POST", "http://llm.test/v1/responses")
    connection = RubricError("기준 생성 API 연결 또는 시간 초과 오류입니다.")
    connection.__cause__ = APIConnectionError(request=request)
    assert (classify(connection).code, classify(connection).retryable) == ("LLM_UNAVAILABLE", True)
    assert classify(MCPError("TRANSPORT_ERROR", "save_rubric")).code == "MCP_UNAVAILABLE"
    assert classify(MCPError("VALIDATION_ERROR", "save_rubric")).code == "RUBRIC_INVALID"
    forbidden = classify(MCPError("FORBIDDEN", "save_rubric"))
    assert (forbidden.code, forbidden.retryable) == ("TOKEN_REJECTED", False)
    assert classify(MCPError("SOMETHING_NEW", "x")).code == "MCP_REJECTED"
    assert classify(KeyError("x")).code == "UNEXPECTED_ERROR"
    assert classify(RubricGenerationError("X", retryable=True)).retryable


@pytest.mark.asyncio
async def test_gateway_allows_only_policy_search_and_rubric_save():
    gateway = RubricMCPGateway("token", business_url="http://b.test/mcp",
                               rag_url="http://r.test/mcp")
    for server, tool in (("business", "get_work_case"), ("rag", "save_rubric"),
                         ("business", "save_evaluation_draft")):
        with pytest.raises(MCPError) as caught:
            await gateway.call(server, tool, {})
        assert caught.value.code == "TOOL_NOT_ALLOWED"


def rubric_token(**claims):
    payload = {"kind": "rubric", "company_id": COMPANY, "exp": int(time.time()) + 3600, **claims}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{raw}.signature"


def test_token_precheck_reads_kind_company_and_expiry():
    assert token_claims(rubric_token())["company_id"] == COMPANY
    assert token_claims("garbage") == {}
    assert cli.worker_company(rubric_token()) == COMPANY
    for bad in (rubric_token(kind="live"), rubric_token(exp=1), "garbage"):
        with pytest.raises(ValueError):
            cli.worker_company(bad)


def test_check_config_reports_names_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (*cli.REQUIRED, "RUBRIC_LLM_API_KEY", "OPENAI_API_KEY", "RUBRIC_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert cli.check_config()["status"] == "incomplete"

    monkeypatch.setenv("RUBRIC_AMQP_URL", "amqp://user:pw@10.0.0.1:5672/")
    monkeypatch.setenv("RUBRIC_BUSINESS_MCP_URL", "http://10.0.0.1:8001/mcp")
    monkeypatch.setenv("RUBRIC_RAG_MCP_URL", "http://10.0.0.1:8002")   # /mcp 빠짐
    monkeypatch.setenv("RUBRIC_MCP_TOKEN", rubric_token())
    monkeypatch.setenv("RUBRIC_LLM_API_KEY", "sk-test")
    result = cli.check_config()
    assert result == {"status": "incomplete", "missing": [], "invalid": ["RUBRIC_RAG_MCP_URL"],
                      "company_id": COMPANY, "network_calls": 0}
    assert "sk-test" not in json.dumps(result) and "pw" not in json.dumps(result)

    monkeypatch.setenv("RUBRIC_RAG_MCP_URL", "http://10.0.0.1:8002/mcp")
    assert cli.check_config()["status"] == "ready"


def test_check_event_cli(tmp_path, capsys):
    # 백엔드 RubricGenerate 메시지와 같은 칸의 샘플
    sample = Path(__file__).resolve().parents[1] / "samples/integration/rubric-generate-v1.json"
    assert cli.main(["check-event", "--event", str(sample)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    path = tmp_path / "event.json"
    path.write_bytes(body(type="EVALUATE"))
    assert cli.main(["check-event", "--event", str(path)]) == 2
