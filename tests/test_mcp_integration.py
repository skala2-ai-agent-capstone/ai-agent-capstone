"""실제 40건을 MCP 응답 모양으로 재생한다. DB/LLM 정확도 테스트는 아니다."""

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from langgraph.types import Command
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from work_classifier.classification.agent import ModelReply
from work_classifier.integrations.mcp_client import MCPError, MCPGateway, unpack_result
from work_classifier.integrations.mcp_data import (
    classification_context,
    fetch_business,
    fetch_policy,
)
from work_classifier.orchestration.graph import MCPHandoff, build_agent_graph
from work_classifier.shared.models import ClassificationResult
from work_classifier.shared.workflow_models import (
    CriteriaDraft,
    Criterion,
    EvaluationOutput,
    ItemEvaluation,
    Reference,
    RuleEvidence,
    ScoreLevel,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = sorted((ROOT / "e2e_raw_40/cases").glob("*/*.json"))
RFP = next(p for p in CASES if p.stem == "E2E-RFP-01")


class ReplayGateway:
    """서버 계약 대역. 서버 저장소/가격 계산의 재구현이 아니라 조회 페이지 테스트용."""
    def __init__(self, path=RFP):
        source = json.loads(path.read_text())
        self.raw = source["raw"]
        rules = json.loads((ROOT / "mcp_servers/data/seed/policy_rules.json").read_text())

        def mask(value):
            if isinstance(value, str):
                for rule in rules["rules"]:
                    if rule["kind"] == "masking":
                        value = re.sub(rule["pattern"], rule["replace_with"], value)
                return value
            if isinstance(value, list):
                return [mask(v) for v in value]
            if isinstance(value, dict):
                return {k: mask(v) for k, v in value.items()}
            return value
        self.raw = mask(self.raw)
        self.wid = self.raw["work_case"]["work_id"]
        target = max((a for a in self.raw["artifact_versions"] if a["author"] == "employee"),
                     key=lambda a: a["saved_at"])
        self.artifacts = [{**a, "work_id": self.wid,
                           "is_evaluation_target": a["version"] == target["version"]}
                          for a in self.raw["artifact_versions"]
                          if a["saved_at"] <= target["saved_at"]]
        self.messages = [m for m in self.raw["messages"]
                         if m["created_at"] <= target["saved_at"]]
        seqs = [m["seq"] for m in self.messages]
        self.snapshot = {
            "artifact_ref": {k: target[k] for k in ("artifact_id", "version", "saved_at")},
            "reference_refs": self.raw["attached_references"], "linked_doc_refs": [],
            "conversation_range": {"from_seq": min(seqs, default=0),
                                   "to_seq": max(seqs, default=0)},
        }
        self.docs = [d for d in self.raw["source_documents"]
                     if d["uploaded_at"] <= target["saved_at"]]
        seeds = json.loads((ROOT / "mcp_servers/data/seed/documents.json").read_text())
        self.policy = next(d for d in seeds if d.get("collection") == "policies")
        self.calls = []

    async def call(self, server, tool, arguments):
        self.calls.append((server, tool, deepcopy(arguments)))
        if tool == "get_work_case":
            data = {**self.raw["work_case"], "source_message": self.raw.get("source_message"),
                    "source_documents": [{k: d.get(k) for k in
                                          ("attachment_id", "doc_id", "version")}
                                         for d in self.docs],
                    "artifact_refs": [{k: a[k] for k in ("artifact_id", "version")}
                                      for a in self.artifacts]}
        elif tool == "get_conversation":
            rows = [m for m in self.messages if
                    arguments["from_seq"] <= m["seq"] <= arguments["to_seq"]]
            data = {"messages": rows[:2], "next_seq": rows[2]["seq"] if len(rows) > 2 else None}
        elif tool == "get_artifact_version":
            data = next(a for a in self.artifacts if a["version"] == arguments["version"])
        elif tool == "get_source_document":
            doc = next(d for d in self.docs if d["attachment_id"] == arguments["attachment_id"])
            lines = [{"line_id": f"{doc['attachment_id']}:L{i:02d}", "text": line}
                     for i, line in enumerate(doc["content"].split("\n"), 1)]
            start = arguments["from_line"] - 1
            data = {**doc, "lines": lines[start:start + 3], "total_lines": len(lines),
                    "next_line": start + 4 if start + 3 < len(lines) else None}
            data.pop("content")
        elif tool == "search_policy":
            chunks = [{**c, "doc_id": self.policy["doc_id"], "version": self.policy["version"],
                       "ref": f"{self.policy['doc_id']}@{self.policy['version']}#{c['chunk_id']}"}
                      for c in self.policy["chunks"]
                      if c["clause_id"] in arguments["clause_ids"]
                      or c["chunk_id"] in arguments["clause_ids"]]
            found = {c["clause_id"] for c in chunks} | {c["chunk_id"] for c in chunks}
            data = {"chunks": chunks, "no_evidence": not chunks,
                    "missing_clause_ids": sorted(set(arguments["clause_ids"]) - found)}
        else:
            assert tool in {"get_linked_works", "get_reference_access_log", "get_evidence_links",
                            "get_policy_check_result", "get_artifact_diff", "get_approval_requests",
                            "compute_reference_quote"}
            data = {"test_double": True}  # 실제 검사·계산 결과로 제시하지 않는다.
        return deepcopy({"ok": True, "data": data,
                         "meta": {"run_id": "test-run", "snapshot": self.snapshot}})


class StubClassifier:
    model = "test-double-not-real-classification"

    def __init__(self, work_type="rfp_analysis", hold=False):
        self.work_type, self.hold, self.calls = work_type, hold, []

    async def classify(self, *, payload, **kwargs):
        data = json.loads(payload)
        self.calls.append(data)
        message = data["messages"][0]
        return ModelReply(ClassificationResult(
            status="needs_review" if self.hold else "classified",
            work_type=None if self.hold else self.work_type,
            candidate_types=[] if self.hold else [self.work_type], reason="테스트 고정 응답",
            evidence=[{"source_id": message["id"], "quote": message["content"][:10]}],
            missing_information=["추가 확인"] if self.hold else [],
        ))


class StubBuilder:
    def __init__(self):
        self.calls = 0

    async def generate(self, work_type, policy, **kwargs):
        self.calls += 1
        chunk = policy.chunks[0]
        source = Reference(document_id=chunk.doc_id, version=chunk.version,
                           section_id=chunk.chunk_id, content=chunk.text)
        evidence = RuleEvidence(document_id=chunk.doc_id, version=chunk.version,
                                section_id=chunk.chunk_id, quote=chunk.text[:10])
        return CriteriaDraft(work_type=work_type, references=[source], items=[Criterion(
            id="test-item", title=f"테스트 초안 {self.calls}", dimension="employee_behavior",
            levels=[ScoreLevel(score=0, condition="테스트 기준")], unobserved_condition="기록 없음",
            rule_evidence=[evidence],
        )])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
async def test_40_cases_preserve_snapshot_and_neutralize_ids(path):
    gateway = ReplayGateway(path)
    packet = await fetch_business(gateway, gateway.wid)
    context, mapping = classification_context(packet)
    assert len(packet.messages) == len(gateway.messages)
    assert len(packet.source_documents) == len(gateway.docs)
    assert len(context.messages) == len(packet.messages)
    assert len([a for a in context.artifacts if a.stage == "final"]) == 1
    payload = context.llm_payload()
    assert "work_type" not in payload and "work_id" not in payload
    assert all(gateway.wid not in m.id for m in context.messages)
    assert mapping[context.messages[0].id]["message_id"] == packet.messages[0]["message_id"]
    assert packet.evidence_sources()
    before = context.model_dump()
    packet.work_case["work_type"] = "incorrect-label"
    assert classification_context(packet)[0].model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", [
    "rfp_analysis", "proposal", "quotation", "customer_response",
])
async def test_seed_policy_supports_each_builder(work_type):
    policy = await fetch_policy(ReplayGateway(), work_type)
    assert len(policy.chunks) > 5  # clause_ids 정확 조회는 top_k=5 제한 예외
    assert all(c.page is None for c in policy.chunks)


def approve(result, action="approve"):
    pending = result["__interrupt__"][0].value
    return {"action": action, **{k: pending[k] for k in (
        "criteria_sha256", "business_sha256", "revision",
    )}}


@pytest.mark.asyncio
async def test_graph_waits_for_human_and_handoff_binds_business():
    gateway, classifier, builder = ReplayGateway(), StubClassifier(), StubBuilder()
    graph = build_agent_graph(gateway, classifier, builder)
    config = {"configurable": {"thread_id": "approval-test"}}
    pending = await graph.ainvoke({"work_id": gateway.wid}, config)
    assert "handoff" not in pending
    assert builder.calls == 1
    assert "__interrupt__" in pending
    calls = len(gateway.calls)
    result = await graph.ainvoke(Command(resume=approve(pending)), config)
    assert result["status"] == "ready_for_evaluation"
    handoff = MCPHandoff.model_validate(result["handoff"])
    assert handoff.business.run_id == handoff.evaluation_input.run_id
    assert len(gateway.calls) == calls  # 승인 재개가 MCP·LLM 호출을 반복하지 않는다.
    assert builder.calls == 1
    bad = deepcopy(result["handoff"])
    bad["business"]["supplemental"]["policy_check"] = {"tampered": True}
    with pytest.raises(ValidationError):
        MCPHandoff.model_validate(bad)


@pytest.mark.asyncio
async def test_revision_and_stale_approval_and_cancel():
    gateway, builder = ReplayGateway(), StubBuilder()
    graph = build_agent_graph(gateway, StubClassifier(), builder)
    config = {"configurable": {"thread_id": "revision-test"}}
    first = await graph.ainvoke({"work_id": gateway.wid}, config)
    second = await graph.ainvoke(Command(resume={**approve(first, "revise"),
                                               "feedback": "기준 구체화"}), config)
    assert builder.calls == 2 and second["revision"] == 2
    assert "handoff" not in second
    invalid = await graph.ainvoke(Command(resume=approve(first)), config)
    assert "이전 초안" in invalid["__interrupt__"][0].value["error"]
    assert "handoff" not in invalid
    result = await graph.ainvoke(Command(resume={"action": "cancel"}), config)
    assert result["status"] == "cancelled" and "handoff" not in result


@pytest.mark.asyncio
async def test_ambiguous_classification_stops_before_criteria():
    gateway, builder = ReplayGateway(), StubBuilder()
    graph = build_agent_graph(gateway, StubClassifier(hold=True), builder)
    result = await graph.ainvoke({"work_id": gateway.wid},
                                {"configurable": {"thread_id": "hold-test"}})
    assert result["status"] == "classification_hold" and builder.calls == 0
    assert not any(server == "rag" for server, _, _ in gateway.calls)


@pytest.mark.asyncio
async def test_quote_calls_authoritative_calculator_only_after_classification():
    gateway = ReplayGateway(next(p for p in CASES if p.stem == "E2E-QT-01"))
    graph = build_agent_graph(gateway, StubClassifier("quotation"), StubBuilder())
    result = await graph.ainvoke({"work_id": gateway.wid},
                                {"configurable": {"thread_id": "quote-test"}})
    assert "__interrupt__" in result
    calls = [args for _, tool, args in gateway.calls if tool == "compute_reference_quote"]
    assert calls == [{"work_id": gateway.wid,
                      "artifact_version": gateway.snapshot["artifact_ref"]["version"]}]


def sdk_result(envelope):
    return SimpleNamespace(is_error=False, structured_content=envelope, content=[])


@pytest.mark.asyncio
async def test_client_retry_limit_and_no_write_tool():
    gateway = MCPGateway("test-token", business_url="unused", rag_url="unused", sleep=AsyncMock())
    temporary = sdk_result({"ok": False, "error": {"code": "TEMPORARY_FAILURE", "retryable": True}})
    client = SimpleNamespace(call_tool=AsyncMock(return_value=temporary))
    gateway._clients["business"] = client
    with pytest.raises(MCPError, match="TEMPORARY_FAILURE"):
        await gateway.call("business", "get_work_case", {})
    assert client.call_tool.await_count == 4
    assert gateway._sleep.await_count == 3
    with pytest.raises(MCPError, match="TOOL_NOT_ALLOWED"):
        await gateway.call("business", "save_evaluation_draft", {})
    assert client.call_tool.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["FORBIDDEN", "VERSION_CONFLICT", "VALIDATION_ERROR", "NOT_FOUND"])
async def test_non_retryable_errors_stop(code):
    gateway = MCPGateway("test-token", business_url="unused", rag_url="unused")
    client = SimpleNamespace(call_tool=AsyncMock(return_value=sdk_result({
        "ok": False, "error": {"code": code, "retryable": False},
    })))
    gateway._clients["business"] = client
    with pytest.raises(MCPError, match=code):
        await gateway.call("business", "get_work_case", {})
    assert client.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_real_sdk_roundtrip_and_snapshot_change_rejected():
    server = MCPServer("contract-test")
    snapshot = {"artifact_ref": {"artifact_id": "a", "version": "v1"}}

    @server.tool()
    async def get_work_case(work_id: str) -> dict:
        return {"ok": True, "data": {"work_id": work_id},
                "meta": {"run_id": "test", "snapshot": snapshot}}

    gateway = MCPGateway("test-token", business_url="unused", rag_url="unused")
    async with Client(server) as client:
        gateway._clients["business"] = client
        result = await gateway.call("business", "get_work_case", {"work_id": "w"})
        assert result["data"]["work_id"] == "w"
        snapshot["artifact_ref"]["version"] = "v2"
        with pytest.raises(MCPError, match="SNAPSHOT_CHANGED"):
            await gateway.call("business", "get_work_case", {"work_id": "w"})


def test_text_envelope_and_invalid_payload():
    result = SimpleNamespace(is_error=False, structured_content=None,
                             content=[SimpleNamespace(type="text", text='{"ok": true}')])
    assert unpack_result(result) == {"ok": True}
    result.content[0].text = "sensitive server error body"
    with pytest.raises(MCPError, match="INVALID_ENVELOPE"):
        unpack_result(result)


@pytest.mark.asyncio
async def test_streamable_http_lifecycle_auth_and_tool_result(monkeypatch):
    server = MCPServer("http-contract-test")

    @server.tool()
    async def get_work_case(work_id: str) -> dict:
        return {"ok": True, "data": {"work_id": work_id},
                "meta": {"run_id": "http-test", "snapshot": {}}}

    app = server.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    auth_headers = []

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            auth_headers.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)

    def http_client(headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi), headers=headers)

    monkeypatch.setattr(
        "work_classifier.integrations.mcp_client.create_mcp_http_client", http_client,
    )
    async with app.router.lifespan_context(app):
        async with MCPGateway("test-secret", business_url="http://testserver/mcp",
                              rag_url="http://testserver/mcp") as gateway:
            result = await gateway.call("business", "get_work_case", {"work_id": "w"})
    assert result["data"]["work_id"] == "w"
    assert auth_headers and all(h == b"Bearer test-secret" for h in auth_headers)


@pytest.mark.asyncio
async def test_injected_evaluator_only_runs_after_approval():
    class StubEvaluator:
        calls = 0

        async def evaluate(self, handoff):
            self.calls += 1
            request = handoff.evaluation_input
            criterion = request.approved_criteria.items[0]
            message = handoff.business.messages[0]
            return EvaluationOutput(
                run_id=request.run_id, criteria_sha256=request.approval.criteria_sha256,
                items=[ItemEvaluation(
                    criterion_id=criterion.id, observation="observed", score=0,
                    reason="테스트 응답", review_required=False,
                    work_evidence=[{"source_id": message["message_id"],
                                    "quote": message["content"][:10]}],
                    rule_evidence=criterion.rule_evidence,
                )], coaching=[], summary="테스트 응답",
            )

    gateway, evaluator = ReplayGateway(), StubEvaluator()
    graph = build_agent_graph(gateway, StubClassifier(), StubBuilder(), evaluator=evaluator)
    config = {"configurable": {"thread_id": "approved-evaluation"}}
    result = await graph.ainvoke({"work_id": gateway.wid}, config)
    assert evaluator.calls == 0
    result = await graph.ainvoke(Command(resume=approve(result)), config)
    assert result["status"] == "completed" and evaluator.calls == 1


@pytest.mark.asyncio
async def test_repeated_page_cursor_fails_instead_of_silent_truncation():
    gateway = ReplayGateway()
    original = gateway.call

    async def broken(server, tool, arguments):
        result = await original(server, tool, arguments)
        if tool == "get_conversation":
            result["data"]["next_seq"] = arguments["from_seq"]
        return result

    gateway.call = broken
    with pytest.raises(MCPError, match="INVALID_PAGINATION"):
        await fetch_business(gateway, gateway.wid)
