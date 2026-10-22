import json
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from ai_agents.cli.demo import DemoClassifier, DemoFixture
from ai_agents.criteria_agent.builder import (
    GUIDANCE,
    ITEM_NAMES,
    GenerationRubric,
    LocalCriteriaPartner,
    OpenAIRubricBuilder,
    ProblemDefinitionRubricRow,
    RubricError,
    WorkTypeRubric,
    adapt_rubric,
    canonical_work_type,
    required_sections,
)
from ai_agents.criteria_agent.policy import PolicyBundle, PolicyChunk
from ai_agents.orchestration.approval import ReviewWorkflow


def fake_policy(work_type):
    return PolicyBundle(chunks=[PolicyChunk(
        doc_id="test-policy", version="test-v1", chunk_id=f"{section}-1",
        clause_id=section, label=section, chapter=None, page=1,
        text=f"① {section}의 검증용 가상 지침이다.",
    ) for section in sorted(required_sections(work_type))])


def fake_rubric(work_type):
    rubric = WorkTypeRubric(work_type=work_type, rows=[{
        "item": item, "criterion": f"{item}의 필수 요소를 준수했는가?",
        "score_2": "관측된 모든 필수 요소를 충족했다.",
        "score_1": "해당 없음" if item == "보안 준수" else "필수 요소 중 일부를 충족했다.",
        "score_0": "관측된 필수 요소를 충족하지 않았다.",
        "unobserved": (
            "업무 등록 및 대화 수집 범위를 확인했으나 해당 항목의 기록 자체가 수집되지 않은 경우."
        ),
        "needs_review": (
            "고객 제출 자료와 연결된 제품 설명서의 사실 정보가 상충해 담당자 확인이 필요한 경우."
        ),
        "evidence_sources": ["업무 등록 내용", "대화"],
        "related_clauses": [*GUIDANCE["work_types"][work_type]["items"][item]["required"], "APX-5"],
    } for item in ITEM_NAMES])
    for field in ["score_0", "score_1", "score_2"]:
        setattr(rubric.rows[0], field, get_args(
            ProblemDefinitionRubricRow.model_fields[field].annotation,
        )[0])
    return rubric


@pytest.mark.parametrize("work_type", list(GUIDANCE["work_types"]))
def test_four_types_map_to_approval_contract_with_exact_evidence(work_type):
    policy, rubric = fake_policy(work_type), fake_rubric(work_type)
    draft = adapt_rubric(rubric, work_type, policy)
    assert len(draft.items) == 5
    assert [level.score for level in draft.items[3].levels] == [0, 2]
    assert [level.score for level in draft.items[0].levels] == [0, 1, 2]
    assert draft.items[3].dimension == "information_handling"
    assert draft.items[4].dimension == "artifact_quality"
    assert all(item.review_condition and item.evidence_sources for item in draft.items)
    sources = {c.chunk_id: c.text for c in policy.chunks}
    for item in draft.items:
        assert all(e.quote == sources[e.section_id] for e in item.rule_evidence)


@pytest.mark.parametrize("legacy, canonical", [
    ("quote", "quotation"), ("inquiry_reply", "customer_response"),
])
def test_notebook_aliases(legacy, canonical):
    assert canonical_work_type(legacy) == canonical
    rubric = fake_rubric(canonical)
    rubric.work_type = legacy
    assert adapt_rubric(rubric, canonical, fake_policy(canonical)).work_type == canonical


def test_generation_schema_rejects_security_middle_score():
    from pydantic import ValidationError

    data = fake_rubric("proposal").model_dump()
    assert GenerationRubric.model_validate(data).rows[3].score_1 == "해당 없음"
    data["rows"][3]["score_1"] = "일부 준수"
    with pytest.raises(ValidationError):
        GenerationRubric.model_validate(data)


@pytest.mark.parametrize("index, score_field, bad_text", [
    (0, "score_1", "고객 미제시로 표시되어 일부 충족이다."),
    (2, "score_2", "모든 오류가 수정되었고 근거도 연결되어야 한다."),
    (2, "score_2", "오류를 수정했고 근거도 연결했다."),
    (2, "unobserved", "AI 초안에 오류가 없거나 대조·수정 기록이 없는 경우."),
])
def test_known_policy_distortions_are_rejected(index, score_field, bad_text):
    rubric = fake_rubric("rfp_analysis")
    setattr(rubric.rows[index], score_field, bad_text)
    with pytest.raises(RubricError):
        adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))


def test_unspecified_exclusion_is_not_mistaken_for_penalty():
    rubric = fake_rubric("rfp_analysis")
    rubric.rows[0].score_1 = "일부 사항이 누락되었다. 고객 미제시로 기재된 경우는 제외된다."
    assert adapt_rubric(rubric, "rfp_analysis", fake_policy("rfp_analysis"))


@pytest.mark.parametrize("bad", ["type", "binary", "unknown", "required", "order", "forbidden"])
def test_bad_rubrics_fail_instead_of_silent_repair(bad):
    rubric, policy = fake_rubric("proposal"), fake_policy("proposal")
    if bad == "type":
        rubric.work_type = "quotation"
    elif bad == "binary":
        rubric.rows[3].score_1 = "일부 준수"
    elif bad == "unknown":
        rubric.rows[0].related_clauses.append("ART-999")
    elif bad == "required":
        rubric.rows[0].related_clauses = ["APX-5"]
    elif bad == "order":
        rubric.rows.reverse()
    else:
        rubric.rows[2].score_2 = "금액 산출 수행"
    with pytest.raises(RubricError):
        adapt_rubric(rubric, "proposal", policy)


def test_paragraph_reference_kept_not_expanded_to_whole_article():
    policy, rubric = fake_policy("rfp_analysis"), fake_rubric("rfp_analysis")
    extra = policy.resolve("ART-11")[0].model_copy(deep=True)
    extra.chunk_id = "ART-11-2"
    extra.text = "② 별도의 항"
    policy.chunks.append(extra)
    rubric.rows[0].related_clauses = ["ART-11-1", "ART-17-1", "APX-5-1"]
    draft = adapt_rubric(rubric, "rfp_analysis", policy)
    assert "ART-11-2" not in {e.section_id for e in draft.items[0].rule_evidence}


def response(rubric):
    return SimpleNamespace(status="completed", output=[], output_parsed=rubric, id="response-test")


@pytest.mark.asyncio
async def test_no_cache_and_revision_feedback_is_forwarded():
    rubric, policy = fake_rubric("quotation"), fake_policy("quotation")
    parse = AsyncMock(return_value=response(rubric))
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    builder = OpenAIRubricBuilder(client, "test")
    draft = await builder.generate("quotation", policy)
    await builder.generate("quotation", policy)
    await builder.generate("quotation", policy, previous=draft, feedback="미관측을 구체화")
    assert parse.await_count == 3
    payload = json.loads(parse.call_args.kwargs["input"][1]["content"])
    assert payload["previous_draft"] == draft.model_dump(mode="json")
    assert payload["human_feedback"] == "미관측을 구체화"
    assert draft.generation["response_id"] == "response-test"
    assert parse.call_args.kwargs["store"] is False


@pytest.mark.asyncio
async def test_generation_only_sees_task_relevant_policy_sections():
    policy = fake_policy("rfp_analysis")
    policy.chunks.append(PolicyChunk(
        doc_id="test-policy", version="test-v1", chunk_id="ART-19-1", clause_id="ART-19",
        label="견적", chapter=None, page=1, text="이 테스트에서 RFP와 관련 없는 견적 조항",
    ))
    parse = AsyncMock(return_value=response(fake_rubric("rfp_analysis")))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    await builder.generate("rfp_analysis", policy)
    payload = json.loads(parse.call_args.kwargs["input"][1]["content"])
    assert "ART-19" not in {c["clause_id"] for c in payload["policy"]["chunks"]}
    assert payload["item_instructions"][0]["focus"]
    assert payload["item_instructions"][0]["required_related_clauses"]


@pytest.mark.asyncio
async def test_bounded_retry_corrects_invalid_type_without_overwriting_it():
    valid = fake_rubric("proposal")
    invalid = valid.model_copy(deep=True)
    invalid.work_type = "quotation"
    parse = AsyncMock(side_effect=[response(invalid), response(valid)])
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    assert (await builder.generate("proposal", fake_policy("proposal"))).work_type == "proposal"
    assert parse.await_count == 2
    assert "업무 유형이 다릅니다" in parse.call_args.kwargs["input"][-1]["content"]
    parse = AsyncMock(return_value=response(invalid))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    with pytest.raises(RubricError, match="검증 실패"):
        await builder.generate("proposal", fake_policy("proposal"))
    assert parse.await_count == 2


@pytest.mark.asyncio
async def test_required_policy_missing_stops_before_api():
    policy = fake_policy("proposal")
    policy.chunks = [c for c in policy.chunks if c.clause_id != "APX-5"]
    parse = AsyncMock()
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    with pytest.raises(RubricError, match="필수 내규"):
        await builder.generate("proposal", policy)
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_real_sdk_structured_parse_and_approval_integration():
    from pathlib import Path

    from ai_agents.classification.agent import classify_work

    fixture = DemoFixture.model_validate_json(
        (Path(__file__).parent.parent / "samples/workflow/rfp_demo.json").read_text()
    )
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True
        assert payload["store"] is False
        return httpx.Response(200, json={
            "id": "response-test", "object": "response", "created_at": 1,
            "status": "completed", "model": "test-model", "output": [{
                "id": "msg-test", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "annotations": [],
                             "text": fake_rubric("rfp_analysis").model_dump_json()}],
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
            partner = LocalCriteriaPartner(
                OpenAIRubricBuilder(client, "test-model"), fake_policy("rfp_analysis"),
            )
            report = await classify_work(
                fixture.work_record, DemoClassifier(fixture.classification),
            )
            flow = ReviewWorkflow(fixture.work_record, report, partner)
            draft = await flow.prepare()
            assert len(draft.items) == 5
            from ai_agents.shared.workflow_models import content_hash

            flow.approve(displayed_hash=content_hash(draft))
            assert flow.approved_input.approved_criteria == draft
            with pytest.raises(RubricError, match="실제 평가기"):
                await flow.evaluate()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, output, parsed", [
    ("incomplete", [], None), ("completed", [], None),
    ("completed", [SimpleNamespace(content=[SimpleNamespace(type="refusal")])], None),
])
async def test_no_fabricated_rubric_on_incomplete_or_refusal(status, output, parsed):
    parse = AsyncMock(return_value=SimpleNamespace(
        status=status, output=output, output_parsed=parsed,
    ))
    builder = OpenAIRubricBuilder(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "test")
    with pytest.raises(RubricError):
        await builder.generate("proposal", fake_policy("proposal"))
    assert parse.await_count == 1
