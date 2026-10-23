from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ai_agents.criteria_agent.builder import canonical_work_type, required_sections
from ai_agents.criteria_agent.policy import PolicyBundle
from ai_agents.integrations.mcp_client import MCPError, MCPGateway
from ai_agents.integrations.mcp_data import fetch_policy
from ai_agents.shared.execution_contracts import DocumentRef


def policy_reply(work_type="rfp_analysis"):
    sections = sorted(
        required_sections(canonical_work_type(work_type)) | {"APX-4", "ART-04", "ART-14"}
    )
    return {
        "ok": True,
        "data": {
            "mode": "clause_ids",
            "chunks": [
                {
                    "ref": f"test-policy@test-v1#{section}-1",
                    "doc_id": "test-policy",
                    "version": "test-v1",
                    "chunk_id": f"{section}-1",
                    "clause_id": section,
                    "label": section,
                    "text": f"① {section}의 가상 지침이다.\n구간 원문을 보존한다.",
                    "content_type": "data",
                    "score": None,
                }
                for section in sections
            ],
            "missing_clause_ids": [],
            "no_evidence": False,
        },
        "meta": {"run_id": "rubric-test-run", "snapshot": {}},
    }


def gateway_for(reply):
    return SimpleNamespace(call=AsyncMock(return_value=deepcopy(reply)))


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", [
    "rfp_analysis", "proposal", "quote", "inquiry_reply", "quotation", "customer_response",
])
async def test_policy_input_accepts_mcp_and_existing_work_type_names(work_type):
    reply = policy_reply(work_type)
    gateway = gateway_for(reply)

    policy = await fetch_policy(gateway, work_type)

    assert isinstance(policy, PolicyBundle)
    assert [c.text for c in policy.chunks] == [c["text"] for c in reply["data"]["chunks"]]
    assert all(c.page is None and c.chapter is None for c in policy.chunks)
    gateway.call.assert_awaited_once()
    server, tool, arguments = gateway.call.await_args.args
    assert (server, tool) == ("rag", "search_policy")
    assert set(arguments["clause_ids"]) >= required_sections(canonical_work_type(work_type))
    assert arguments["top_k"] == 5
    assert len(policy.chunks) > 5  # 조항 정확 조회를 검색 상위 5개로 자르지 않는다.


@pytest.mark.asyncio
async def test_expected_policy_ref_matches_without_becoming_a_tool_argument():
    gateway = gateway_for(policy_reply())
    expected = DocumentRef(doc_id="test-policy", version="test-v1")

    policy = await fetch_policy(gateway, "rfp_analysis", expected_policy=expected)

    assert all((c.doc_id, c.version) == (expected.doc_id, expected.version)
               for c in policy.chunks)
    assert set(gateway.call.await_args.args[2]) == {"clause_ids", "top_k"}


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,code", [
    (DocumentRef(doc_id="different-policy", version="test-v1"), "REFERENCE_MISMATCH"),
    (DocumentRef(doc_id="test-policy", version="test-v2"), "VERSION_CONFLICT"),
])
async def test_policy_ref_mismatch_fails_without_latest_version_fallback(expected, code):
    gateway = gateway_for(policy_reply())

    with pytest.raises(MCPError) as error:
        await fetch_policy(gateway, "rfp_analysis", expected_policy=expected)

    assert (error.value.code, error.value.tool) == (code, "search_policy")
    gateway.call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_evidence", "reported_missing", "unreported_missing", "empty"])
async def test_missing_policy_is_rejected_even_if_server_omits_missing_flag(case):
    reply = policy_reply()
    if case == "no_evidence":
        reply["data"]["no_evidence"] = True
    elif case == "reported_missing":
        reply["data"]["missing_clause_ids"] = ["APX-5"]
    elif case == "unreported_missing":
        reply["data"]["chunks"] = [
            c for c in reply["data"]["chunks"] if c["clause_id"] != "APX-5"
        ]
    else:
        reply["data"]["chunks"] = []

    with pytest.raises(MCPError) as error:
        await fetch_policy(gateway_for(reply), "rfp_analysis")

    assert (error.value.code, error.value.tool) == ("POLICY_INCOMPLETE", "search_policy")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("version", "test-v2"), ("doc_id", "other-policy")])
async def test_mixed_policy_sources_fail_before_generation(field, value):
    reply = policy_reply()
    chunk = reply["data"]["chunks"][0]
    chunk[field] = value
    chunk["ref"] = f"{chunk['doc_id']}@{chunk['version']}#{chunk['chunk_id']}"

    with pytest.raises(MCPError) as error:
        await fetch_policy(gateway_for(reply), "rfp_analysis")

    assert error.value.code == "VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_forged_reference_is_rejected():
    reply = policy_reply()
    reply["data"]["chunks"][0]["ref"] = "other-policy@test-v1#APX-5"

    with pytest.raises(MCPError, match="REFERENCE_MISMATCH"):
        await fetch_policy(gateway_for(reply), "rfp_analysis")


@pytest.mark.asyncio
async def test_duplicate_chunks_are_not_silently_deduplicated():
    reply = policy_reply()
    reply["data"]["chunks"].append(deepcopy(reply["data"]["chunks"][0]))

    with pytest.raises(ValidationError, match="중복"):
        await fetch_policy(gateway_for(reply), "rfp_analysis")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "NOT_FOUND", "FORBIDDEN", "VERSION_CONFLICT", "TEMPORARY_FAILURE",
])
async def test_gateway_errors_are_not_converted_to_an_empty_policy(code):
    gateway = SimpleNamespace(call=AsyncMock(side_effect=MCPError(code, "search_policy")))

    with pytest.raises(MCPError) as error:
        await fetch_policy(gateway, "rfp_analysis")

    assert error.value.code == code
    gateway.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_work_type_is_rejected_before_tool_call():
    gateway = gateway_for(policy_reply())

    with pytest.raises(ValidationError):
        await fetch_policy(gateway, "unsupported")

    gateway.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_fetch_through_gateway_accepts_empty_rubric_snapshot():
    reply = policy_reply("quote")
    sdk_result = SimpleNamespace(is_error=False, structured_content=reply, content=[])
    client = SimpleNamespace(call_tool=AsyncMock(return_value=sdk_result))
    gateway = MCPGateway("test-token", business_url="unused", rag_url="unused")
    gateway._clients["rag"] = client

    policy = await fetch_policy(gateway, "quote")

    assert policy.chunks and gateway._scope == reply["meta"]
    client.call_tool.assert_awaited_once()
