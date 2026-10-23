import json

import pytest
from pydantic import ValidationError
from test_criteria_agent import builder_for
from test_criteria_mcp_input import gateway_for, policy_reply
from test_rubric_builder import fake_policy, fake_rubric

from ai_agents.criteria_agent.agent import generate_mcp_criteria
from ai_agents.criteria_agent.builder import RubricError, adapt_rubric, canonical_work_type
from ai_agents.criteria_agent.mcp_output import to_mcp_rubric
from ai_agents.shared.execution_contracts import DocumentRef

WORK_TYPES = [
    ("rfp_analysis", "rfp_analysis"), ("proposal", "proposal"),
    ("quotation", "quote"), ("customer_response", "inquiry_reply"),
    ("quote", "quote"), ("inquiry_reply", "inquiry_reply"),
]


@pytest.mark.parametrize("work_type,external", WORK_TYPES)
def test_conversion_preserves_all_item_fields_with_mcp_work_type(work_type, external):
    canonical = canonical_work_type(work_type)
    policy, rubric = fake_policy(canonical), fake_rubric(canonical)
    draft = adapt_rubric(rubric, canonical, policy)
    before = draft.model_dump(mode="json")

    payload = to_mcp_rubric(draft, policy)

    assert payload == {
        "work_type": external, "items": [row.model_dump(mode="json") for row in rubric.rows],
    }
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert draft.model_dump(mode="json") == before


def test_only_title_prefix_is_removed_and_score_whitespace_is_preserved():
    policy, rubric = fake_policy("rfp_analysis"), fake_rubric("rfp_analysis")
    rubric.rows[1].criterion = "업무 조건: 요청 범위와 결과 형식을 명시했는가?"
    rubric.rows[1].score_2 = "요청 범위와  결과 형식을\n모두 명시했다."
    draft = adapt_rubric(rubric, "rfp_analysis", policy)

    item = to_mcp_rubric(draft, policy)["items"][1]

    assert item["criterion"] == rubric.rows[1].criterion
    assert item["score_2"] == rubric.rows[1].score_2


@pytest.mark.parametrize("specific_paragraph", [False, True])
def test_paragraph_links_use_real_clause_ids_without_losing_internal_evidence(specific_paragraph):
    policy, rubric = fake_policy("rfp_analysis"), fake_rubric("rfp_analysis")
    extra = policy.resolve("ART-11")[0].model_copy(deep=True)
    extra.chunk_id, extra.text = "ART-11-2", "② 두 번째 항의 별도 원문"
    policy.chunks.append(extra)
    if specific_paragraph:
        rubric.rows[0].related_clauses = ["ART-11-2", "ART-17-1", "APX-5-1"]
    draft = adapt_rubric(rubric, "rfp_analysis", policy)
    before = draft.model_dump(mode="json")

    clauses = to_mcp_rubric(draft, policy)["items"][0]["related_clauses"]

    assert clauses == ["ART-11", "ART-17", "APX-5"]
    assert clauses.count("ART-11") == 1
    evidence_ids = [e.section_id for e in draft.items[0].rule_evidence]
    assert "ART-11-2" in evidence_ids
    assert ("ART-11-1" in evidence_ids) is not specific_paragraph
    assert draft.model_dump(mode="json") == before


def test_chunks_with_article_ids_are_not_truncated():
    policy, rubric = fake_policy("rfp_analysis"), fake_rubric("rfp_analysis")
    for chunk in policy.chunks:
        chunk.chunk_id = chunk.clause_id
    draft = adapt_rubric(rubric, "rfp_analysis", policy)

    payload = to_mcp_rubric(draft, policy)

    assert payload["items"][0]["related_clauses"] == ["ART-11", "ART-17", "APX-5"]


def test_warnings_and_internal_metadata_are_not_added_to_storage_fields():
    policy, rubric = fake_policy("rfp_analysis"), fake_rubric("rfp_analysis")
    rubric.rows[4].criterion = "예산 금액 조건이 최종 산출물에 정확히 반영됐는지 평가한다"
    draft = adapt_rubric(rubric, "rfp_analysis", policy, generation={"model": "test-model"})
    before = draft.model_dump(mode="json")
    assert draft.warnings and draft.references and draft.generation

    payload = to_mcp_rubric(draft, policy)

    assert set(payload) == {"work_type", "items"}
    expected_keys = {
        "item", "criterion", "score_0", "score_1", "score_2", "unobserved", "needs_review",
        "evidence_sources", "related_clauses",
    }
    assert all(set(item) == expected_keys for item in payload["items"])
    assert payload["items"][4]["criterion"] == rubric.rows[4].criterion
    assert draft.model_dump(mode="json") == before


@pytest.mark.parametrize("case", ["missing", "reordered", "duplicate", "id", "title", "criterion"])
def test_invalid_item_layout_is_not_silently_repaired(case):
    policy = fake_policy("rfp_analysis")
    draft = adapt_rubric(fake_rubric("rfp_analysis"), "rfp_analysis", policy)
    if case == "missing":
        draft.items.pop()
    elif case == "reordered":
        draft.items.reverse()
    elif case == "duplicate":
        draft.items[1] = draft.items[0].model_copy(deep=True)
    elif case == "id":
        draft.items[1].id = "proposal.instruction_quality"
    elif case == "title":
        draft.items[1].title = "결과 검증: 다른 항목의 제목"
    else:
        draft.items[1].title = "지시 품질: "

    with pytest.raises((RubricError, ValidationError)):
        to_mcp_rubric(draft, policy)


@pytest.mark.parametrize("case", [
    "missing_zero", "missing_middle", "missing_two", "extra", "security_middle", "duplicate",
])
def test_invalid_score_layout_fails_before_conversion(case):
    policy = fake_policy("rfp_analysis")
    draft = adapt_rubric(fake_rubric("rfp_analysis"), "rfp_analysis", policy)
    item = draft.items[1]
    if case.startswith("missing_"):
        score = {"missing_zero": 0, "missing_middle": 1, "missing_two": 2}[case]
        item.levels = [level for level in item.levels if level.score != score]
    elif case == "extra":
        item.levels.append(item.levels[0].model_copy(update={"score": 3}))
    elif case == "security_middle":
        draft.items[3].levels.append(item.levels[1].model_copy(deep=True))
    else:
        item.levels[1].condition = item.levels[0].condition.replace(" ", "  ")

    with pytest.raises(RubricError, match="점수"):
        to_mcp_rubric(draft, policy)


@pytest.mark.parametrize("field,value", [
    ("document_id", "other-policy"), ("version", "test-v2"),
    ("section_id", "ART-999-1"), ("content", "입력 내규에 없는 위조 원문"),
])
def test_self_consistent_but_forged_sources_are_rejected_against_input_policy(field, value):
    policy = fake_policy("rfp_analysis")
    draft = adapt_rubric(fake_rubric("rfp_analysis"), "rfp_analysis", policy)
    ref = draft.references[0]
    original_key = (ref.document_id, ref.version, ref.section_id)
    setattr(ref, field, value)
    for item in draft.items:
        for evidence in item.rule_evidence:
            if evidence.key() == original_key:
                setattr(evidence, "quote" if field == "content" else field, value)

    with pytest.raises(RubricError, match="입력 내규와 다릅니다"):
        to_mcp_rubric(draft, policy)


def test_missing_required_clause_is_rejected_even_with_valid_remaining_references():
    policy = fake_policy("rfp_analysis")
    draft = adapt_rubric(fake_rubric("rfp_analysis"), "rfp_analysis", policy)
    draft.items[0].rule_evidence = [
        e for e in draft.items[0].rule_evidence if e.section_id != "ART-11-1"
    ]

    with pytest.raises(RubricError, match="필수 근거 누락"):
        to_mcp_rubric(draft, policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type,external", WORK_TYPES)
async def test_mcp_generation_returns_json_with_one_policy_read_and_no_storage(work_type, external):
    canonical = canonical_work_type(work_type)
    rubric = fake_rubric(canonical)
    builder, parse = builder_for(rubric)
    gateway = gateway_for(policy_reply(work_type))

    payload = await generate_mcp_criteria(
        gateway, builder, work_type,
        expected_policy=DocumentRef(doc_id="test-policy", version="test-v1"),
    )

    assert payload == {
        "work_type": external, "items": [row.model_dump(mode="json") for row in rubric.rows],
    }
    parse.assert_awaited_once()
    gateway.call.assert_awaited_once()
    assert gateway.call.await_args.args[:2] == ("rag", "search_policy")
