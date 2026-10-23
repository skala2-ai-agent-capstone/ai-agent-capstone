import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_criteria_mcp_input import gateway_for, policy_reply
from test_rubric_builder import fake_rubric, response

from ai_agents.criteria_agent.agent import generate_criteria, generate_mcp_criteria
from ai_agents.criteria_agent.builder import (
    ITEM_NAMES,
    OpenAIRubricBuilder,
    RubricError,
    canonical_work_type,
)
from ai_agents.integrations.mcp_client import MCPError
from ai_agents.shared.execution_contracts import DocumentRef
from ai_agents.shared.workflow_models import CriteriaDraft


def builder_for(rubric):
    parse = AsyncMock(return_value=response(rubric))
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    return OpenAIRubricBuilder(client, "test-model", max_retries=0), parse


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_standalone_generation_returns_validated_criteria_without_work_record(work_type):
    canonical = canonical_work_type(work_type)
    reply = policy_reply(work_type)
    gateway = gateway_for(reply)
    builder, parse = builder_for(fake_rubric(canonical))

    draft = await generate_criteria(
        gateway,
        builder,
        work_type,
        expected_policy=DocumentRef(doc_id="test-policy", version="test-v1"),
    )

    assert draft.work_type == canonical
    assert [item.title.split(":", 1)[0] for item in draft.items] == ITEM_NAMES
    assert [level.score for level in draft.items[3].levels] == [0, 2]
    sources = {chunk["chunk_id"]: chunk["text"] for chunk in reply["data"]["chunks"]}
    assert draft.references
    for ref in draft.references:
        assert (ref.document_id, ref.version) == ("test-policy", "test-v1")
        assert ref.content == sources[ref.section_id]
    assert CriteriaDraft.model_validate_json(draft.model_dump_json()) == draft
    assert draft.generation["response_id"] == "response-test"
    parse.assert_awaited_once()
    payload = json.loads(parse.await_args.kwargs["input"][1]["content"])
    assert payload["work_type"] == canonical
    assert payload["previous_draft"] is None
    assert payload["human_feedback"] is None
    # Employee 조회·분류·승인·저장 없이 내규 조회 한 번과 생성으로 완료된다.
    gateway.call.assert_awaited_once()
    assert gateway.call.await_args.args[:2] == ("rag", "search_policy")


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [generate_criteria, generate_mcp_criteria])
@pytest.mark.parametrize("case,code", [
    ("missing_clause", "POLICY_INCOMPLETE"),
    ("wrong_version", "VERSION_CONFLICT"),
])
async def test_policy_failure_stops_before_model_generation(generate, case, code):
    reply = policy_reply()
    if case == "missing_clause":
        reply["data"]["chunks"] = [
            chunk for chunk in reply["data"]["chunks"] if chunk["clause_id"] != "APX-5"
        ]
    gateway = gateway_for(reply)
    builder, parse = builder_for(fake_rubric("rfp_analysis"))
    expected = DocumentRef(
        doc_id="test-policy", version="test-v2" if case == "wrong_version" else "test-v1",
    )

    with pytest.raises(MCPError) as error:
        await generate(gateway, builder, "rfp_analysis", expected_policy=expected)

    assert error.value.code == code
    parse.assert_not_awaited()
    gateway.call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [generate_criteria, generate_mcp_criteria])
@pytest.mark.parametrize("code", ["FORBIDDEN", "NOT_FOUND", "TEMPORARY_FAILURE"])
async def test_mcp_failure_propagates_without_generating_or_storing_a_result(generate, code):
    gateway = SimpleNamespace(call=AsyncMock(side_effect=MCPError(code, "search_policy")))
    builder, parse = builder_for(fake_rubric("rfp_analysis"))

    with pytest.raises(MCPError) as error:
        await generate(gateway, builder, "rfp_analysis")

    assert error.value.code == code
    parse.assert_not_awaited()
    gateway.call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [generate_criteria, generate_mcp_criteria])
async def test_invalid_generated_evidence_is_not_returned_as_a_success(generate):
    gateway = gateway_for(policy_reply())
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[0].related_clauses = ["ART-999"]
    builder, parse = builder_for(rubric)

    with pytest.raises(RubricError, match="검증 실패"):
        await generate(gateway, builder, "rfp_analysis")

    parse.assert_awaited_once()
    gateway.call.assert_awaited_once()
    assert gateway.call.await_args.args[:2] == ("rag", "search_policy")


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [generate_criteria, generate_mcp_criteria])
async def test_invalid_work_type_does_not_call_mcp_or_model(generate):
    gateway = gateway_for(policy_reply())
    builder, parse = builder_for(fake_rubric("rfp_analysis"))

    with pytest.raises(ValidationError):
        await generate(gateway, builder, "unsupported")

    gateway.call.assert_not_awaited()
    parse.assert_not_awaited()
