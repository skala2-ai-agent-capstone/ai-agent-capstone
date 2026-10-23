import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
from test_mcp_adapter import CASES, approved_request, inputs, policy_response, response

from ai_agents.evaluation_agent.final.agent import OpenAIEvaluator, render_evaluation_report
from ai_agents.evaluation_agent.final.materials import collect_evaluation_materials
from ai_agents.evaluation_agent.final.models import AgentEvaluationOutput
from ai_agents.evaluation_agent.final.validation import EvaluationError, validate_evaluation_result
from ai_agents.integrations.mcp_adapter import collect_evaluation_evidence
from ai_agents.shared.llm_trace import work_scope
from ai_agents.shared.workflow_models import content_hash


class ToolFixture:
    """raw 자료의 전송 형식과 RAG 응답 대역. 실제 서버·API는 호출하지 않는다."""

    def __init__(self, work_type="proposal", *, case=None):
        self.case = case or next(c for c in CASES if c["work_type"] == work_type)
        work_type = self.case["work_type"]
        self.raw = deepcopy(self.case["raw"])
        self.responses = inputs(self.case)
        self.context = self.responses["context"]
        self.request = approved_request(self.context)
        canonical = {"quote": "quotation", "inquiry_reply": "customer_response"}.get(
            work_type,
            work_type,
        )
        self.request.classification.work_type = canonical
        self.request.classification.candidate_types = [canonical]
        self.request.approved_criteria.work_type = canonical
        for criterion in self.request.approved_criteria.items:
            criterion.id = f"{canonical}.{criterion.id.rsplit('.', 1)[1]}"
        self.request.approval.criteria_sha256 = content_hash(self.request.approved_criteria)
        self.responses["work_case_response"]["data"]["source_documents"] = [
            {k: doc[k] for k in ("attachment_id", "doc_id", "version")}
            for doc in self.raw["source_documents"]
        ]
        self.calls = []
        self.reference_status = "현행"

    async def business(self, name, arguments):
        self.calls.append(("business", name, arguments))
        if name == "get_work_case":
            return self.responses["work_case_response"]
        if name == "get_artifact_version":
            return next(
                r
                for r in self.responses["artifact_responses"]
                if (r["data"]["artifact_id"], r["data"]["version"])
                == (arguments["artifact_id"], arguments["version"])
            )
        if name == "get_policy_check_result":
            env = policy_response(self.responses)
            env["data"]["scope"]["user_message_ids"] = [
                m["message_id"]
                for m in self.raw["messages"]
                if m["role"] == "user"
                and self.context.snapshot["conversation_range"]["from_seq"]
                <= m["seq"]
                <= self.context.snapshot["conversation_range"]["to_seq"]
            ]
            return env
        if name == "get_conversation":
            messages = [
                m
                for m in self.raw["messages"]
                if arguments["from_seq"] <= m["seq"] <= arguments["to_seq"]
            ]
            size = arguments["limit"]
            data = {
                "work_id": self.context.work_id,
                "conversation_range": self.context.snapshot["conversation_range"],
                "messages": messages[:size],
                "next_seq": messages[size]["seq"] if len(messages) > size else None,
            }
        elif name == "get_reference_access_log":
            data = {
                "work_id": self.context.work_id,
                "accesses": [
                    {
                        **record,
                        "access_id": f"{self.context.work_id}:ACC-{i:02d}",
                    }
                    for i, record in enumerate(self.raw["reference_access_log"], 1)
                ],
            }
        elif name == "get_evidence_links":
            data = {"work_id": self.context.work_id, "links": self.raw["evidence_links"]}
        elif name == "get_approval_requests":
            data = {
                "work_id": self.context.work_id,
                "requests": self.raw.get("approval_requests", []),
            }
        elif name == "get_artifact_diff":
            data = {**arguments, "changes": []}
        elif name == "compute_reference_quote":
            data = {
                "work_id": self.context.work_id,
                "artifact_version": arguments["artifact_version"],
                "evidence_id": f"{self.context.work_id}:reference_quote",
                "total": 1000,
            }
        elif name == "get_source_document":
            doc = next(
                d
                for d in self.raw["source_documents"]
                if d["attachment_id"] == arguments["attachment_id"]
            )
            lines = doc["content"].split("\n")
            start, size = arguments["from_line"], arguments["limit"]
            data = {
                "attachment_id": doc["attachment_id"],
                "doc_id": doc["doc_id"],
                "version": doc["version"],
                "total_lines": len(lines),
                "lines": [
                    {"line_id": f"{doc['attachment_id']}:L{i:02d}", "text": text}
                    for i, text in enumerate(lines[start - 1 : start - 1 + size], start)
                ],
                "next_line": start + size if start + size <= len(lines) else None,
            }
        else:
            raise AssertionError(f"unexpected tool: {name}")
        return response(self.context, data)

    async def rag(self, name, arguments):
        self.calls.append(("rag", name, arguments))
        assert name == "get_reference"
        if "chunk_id" in arguments:
            ref = next(
                r
                for r in self.request.approved_criteria.references
                if r.document_id == arguments["doc_id"]
            )
            data = {
                "doc_id": ref.document_id,
                "version": ref.version,
                "chunk_id": ref.section_id,
                "text": ref.content,
                "ref": f"{ref.document_id}@{ref.version}#{ref.section_id}",
            }
        else:
            data = {
                **arguments,
                "status": self.reference_status,
                "grade": "공개",
                "chunks": [
                    {
                        "ref": f"{arguments['doc_id']}@{arguments['version']}#S1",
                        "chunk_id": "S1",
                        "text": "공식 자료의 테스트 원문",
                    }
                ],
            }
        return response(self.context, data)


async def collect(fixture):
    evidence = await collect_evaluation_evidence(
        context=fixture.context, call_tool=fixture.business
    )
    materials = await collect_evaluation_materials(
        evidence=evidence,
        criteria=fixture.request.approved_criteria,
        business_call=fixture.business,
        rag_call=fixture.rag,
    )
    return evidence, materials


def valid_result(fixture, evidence, materials):
    employee = next(m for m in fixture.raw["messages"] if m["role"] == "user")
    target = next(a for a in evidence.artifacts if a.is_evaluation_target)
    items = []
    for criterion in fixture.request.approved_criteria.items:
        key = criterion.id.rsplit(".", 1)[1]
        source_id, quote = employee["message_id"], employee["content"]
        if key == "quality":
            source_id, quote = target.spans[0].span_id, target.spans[0].text
        elif key == "security":
            source_id = evidence.policy_check.evidence_id
            quote = evidence.evidence_sources()[source_id]
        items.append(
            {
                "criterion_id": criterion.id,
                "observation": "observed",
                "score": 0,
                "reason": "테스트의 관측 근거와 기준을 대조했습니다.",
                "work_evidence": [{"source_id": source_id, "quote": quote}],
                "rule_evidence": [criterion.rule_evidence[0].model_dump(mode="json")],
                "review_required": False,
            }
        )
    return AgentEvaluationOutput.model_validate(
        {
            "run_id": fixture.context.run_id,
            "criteria_sha256": fixture.request.approval.criteria_sha256,
            "items": items,
            "coaching": [
                {"criterion_id": i["criterion_id"], "action": "공식 자료와 비교한 기록을 남기세요."}
                for i in items
            ],
            "summary": "이 임의 요약은 프로그램이 관측 점수만으로 다시 계산합니다.",
        }
    )


def selected_result(result, payload):
    """고정 공개 결과를 모델 전용 필수/추가 근거 선택 응답으로 바꾼다."""
    checked = result.model_dump(mode="json")
    items = []
    for item in checked["items"]:
        requirements = payload["criterion_requirements"][item["criterion_id"]]
        work_ids = [
            next(row["id"] for row in payload["work_evidence_catalog"]
                 if row["source_id"] == proof["source_id"] and row["text"] in proof["quote"])
            for proof in item.pop("work_evidence")
        ]
        rule_ids = [
            next(row["id"] for row in payload["reference_catalog"]
                 if all(row[key] == proof[key] for key in ("document_id", "version", "section_id"))
                 and row["content"] in proof["quote"])
            for proof in item.pop("rule_evidence")
        ]
        primary_work = next(
            (key for key in work_ids if key in requirements["required_work_choice_ids"]), None,
        ) if item["observation"] == "observed" else next(iter(work_ids), None)
        primary_rule = next(
            key for key in rule_ids if key in requirements["required_rule_choice_ids"]
        )
        item["required_work_evidence_id"] = primary_work
        item["work_evidence_ids"] = [key for key in work_ids if key != primary_work]
        item["required_rule_evidence_id"] = primary_rule
        item["rule_evidence_ids"] = [key for key in rule_ids if key != primary_rule]
        items.append(item)
    return {"items": items, "coaching": checked["coaching"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_four_sales_types_collect_validate_and_render(work_type):
    fixture = ToolFixture(work_type)
    evidence, materials = await collect(fixture)
    checked = validate_evaluation_result(
        fixture.request,
        valid_result(fixture, evidence, materials),
        evidence=evidence,
        materials=materials,
    )
    assert len(checked.items) == len(checked.coaching) == 5
    assert "5개 항목 중 5개 채점" in checked.summary
    assert "코칭:" in render_evaluation_report(checked)
    assert any(server == "rag" for server, _, _ in fixture.calls)
    assert any(name == "compute_reference_quote" for _, name, _ in fixture.calls) == (
        work_type == "quote"
    )
    assert all(not name.startswith(("save_", "request_review")) for _, name, _ in fixture.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_supplemental_collection_is_compatible_with_all_40_cases(case):
    fixture = ToolFixture(case=case)
    evidence, materials = await collect(fixture)
    assert materials.context.work_id == evidence.context.work_id
    assert materials.evidence_sha256 == content_hash(evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "run",
        "hash",
        "criterion",
        "unknown_work_id",
        "wrong_quote",
        "rule_version",
        "rule_quote",
        "coaching",
        "employee_proof",
        "target_proof",
        "security_proof",
    ],
)
async def test_invalid_or_unsupported_model_results_are_rejected(change):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    result = valid_result(fixture, evidence, materials)
    if change == "run":
        result.run_id = "OTHER"
    elif change == "hash":
        result.criteria_sha256 = "OTHER"
    elif change == "criterion":
        result.items[0].criterion_id = "proposal.unknown"
    elif change == "unknown_work_id":
        result.items[0].work_evidence[0].source_id = "INVENTED"
    elif change == "wrong_quote":
        result.items[0].work_evidence[0].quote = "원문에 없는 인용"
    elif change == "rule_version":
        result.items[0].rule_evidence[0].version = "v99"
    elif change == "rule_quote":
        result.items[0].rule_evidence[0].quote = "없는 기준 문장"
    elif change == "coaching":
        result.coaching[0].criterion_id = result.coaching[1].criterion_id
    elif change == "employee_proof":
        result.items[0].work_evidence = deepcopy(result.items[4].work_evidence)
    elif change == "target_proof":
        result.items[4].work_evidence = deepcopy(result.items[0].work_evidence)
    else:
        result.items[3].work_evidence = deepcopy(result.items[0].work_evidence)
    with pytest.raises(EvaluationError):
        validate_evaluation_result(fixture.request, result, evidence=evidence, materials=materials)


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_sdk_serialization_parsing_and_validation_offline(work_type):
    fixture = ToolFixture(work_type)
    evidence, materials = await collect(fixture)
    result = valid_result(fixture, evidence, materials)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "gpt-4o-mini"
        assert body["store"] is False
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        payload = json.loads(body["input"][1]["content"])
        assert payload["work_evidence_catalog"]
        assert payload["reference_catalog"]
        assert payload["security_evidence"]["available"] is True
        assert payload["security_evidence"]["policy_check_choice_ids"]
        # SDK가 실제 전송하는 형식에서도 채점 상태와 점수·근거의 모순을 막는다.
        schema = body["text"]["format"]["schema"]
        assert schema["type"] == "object" and "anyOf" not in schema
        validator = Draft202012Validator(schema)
        wire = selected_result(result, payload)
        assert validator.is_valid(wire)
        general_work = next(
            row["id"] for row in payload["work_evidence_catalog"] if row["kind"] == "message:user"
        )
        for change in (
            {"score": None}, {"required_work_evidence_id": None}, {"score": 1},
            {"required_work_evidence_id": general_work},
        ):
            broken = deepcopy(wire)
            broken["items"][3].update(change)
            assert not validator.is_valid(broken)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-4o-mini",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    selected_result(result, payload), ensure_ascii=False
                                ),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with AsyncOpenAI(
            api_key="fake-test-key", http_client=http_client, max_retries=0
        ) as client:
            agent = OpenAIEvaluator(
                client,
                context=fixture.context,
                business_call=fixture.business,
                rag_call=fixture.rag,
            )
            checked = await agent.evaluate(fixture.request)
    assert len(requests) == 1
    assert "관측 항목 합계 0/10점" in checked.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5))
async def test_actual_sdk_reports_failed_values_and_parses_corrected_result_offline(index):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        payload = json.loads(body["input"][1]["content"])
        wire = selected_result(good, payload)
        if len(requests) == 1:
            wire["items"][index].update(
                score=None, required_work_evidence_id=None, work_evidence_ids=[],
            )
        else:
            feedback = body["input"][-1]["content"]
            assert good.items[index].criterion_id in feedback
            assert '"score": null' in feedback
            assert '"work_evidence_ids": []' in feedback
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-4o-mini",
            "output": [{
                "id": "msg_offline", "type": "message", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": json.dumps(wire, ensure_ascii=False),
                    "annotations": [],
                }],
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with AsyncOpenAI(
            api_key="fake-test-key", http_client=http_client, max_retries=0,
        ) as client:
            agent = OpenAIEvaluator(
                client, context=fixture.context, business_call=fixture.business,
                rag_call=fixture.rag,
            )
            checked = await agent.evaluate_collected(
                fixture.request, evidence=evidence, materials=materials,
            )
    assert len(requests) == 2
    assert checked.items[index].score == 0
    assert checked.items[index].work_evidence


@pytest.mark.asyncio
async def test_invalid_evidence_selection_gets_one_bounded_retry():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)
    replies = 0

    async def reply(**arguments):
        nonlocal replies
        replies += 1
        payload = json.loads(arguments["input"][1]["content"])
        raw = selected_result(good, payload)
        if replies == 1:
            raw["items"][0]["required_work_evidence_id"] = "w-nonexistent"
            parsed = SimpleNamespace(
                model_dump=lambda **_: raw, model_dump_json=lambda: json.dumps(raw)
            )
        else:
            parsed = arguments["text_format"].model_validate(raw)
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context,
        business_call=fixture.business,
        rag_call=fixture.rag,
    )
    work_id = fixture.request.work_record.work_id
    with work_scope(work_id):
        result = await agent.evaluate_collected(
            fixture.request, evidence=evidence, materials=materials,
        )
    assert parse.await_count == 2
    assert result.run_id == fixture.context.run_id
    # 재요청까지 같은 업무로 기록되도록 매 호출에 기록 프록시용 헤더를 붙인다
    assert [call.kwargs["extra_headers"] for call in parse.await_args_list] == [
        {"X-Stage": "evaluate", "X-Work-Id": work_id},
    ] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "reason"), [
    ({"score": 1}, "보안 준수 항목에는 1점을 줄 수 없습니다"),
    ({"observation": "unobserved"}, "미관측·자료 부족 항목에는 점수를 부여할 수 없습니다"),
    ({"score": None}, "관측된 항목에는 점수와 업무 근거가 필요합니다"),
    ({"required_work_evidence_id": None}, "관측된 항목에는 점수와 업무 근거가 필요합니다"),
    ({"observation": "needs_review", "score": None, "review_required": False},
     "검토 필요 항목은 review_required가 true여야 합니다"),
])
async def test_item_3_failure_keeps_actual_reason_in_output_and_retry(change, reason):
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)

    async def reply(**arguments):
        payload = json.loads(arguments["input"][1]["content"])
        raw = selected_result(good, payload)
        raw["items"][3].update(change)
        parsed = arguments["text_format"].model_validate(raw)
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context, business_call=fixture.business, rag_call=fixture.rag,
    )
    with pytest.raises(EvaluationError) as caught:
        await agent.evaluate_collected(fixture.request, evidence=evidence, materials=materials)
    assert f"items.3: {reason}" in str(caught.value)
    assert parse.await_count == 2  # 기존 최대 1회 수정 요청은 그대로 유지한다.
    feedback = parse.await_args_list[1].kwargs["input"][-1]["content"]
    assert reason in feedback


@pytest.mark.asyncio
async def test_item_semantics_can_be_corrected_using_specific_retry_feedback():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)
    calls = 0

    async def reply(**arguments):
        nonlocal calls
        calls += 1
        payload = json.loads(arguments["input"][1]["content"])
        raw = selected_result(good, payload)
        if calls == 1:
            raw["items"][3]["score"] = 1
        else:
            assert "보안 준수 항목에는 1점을 줄 수 없습니다" in arguments["input"][-1]["content"]
        parsed = arguments["text_format"].model_validate(raw)
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context, business_call=fixture.business, rag_call=fixture.rag,
    )
    result = await agent.evaluate_collected(fixture.request, evidence=evidence, materials=materials)
    assert result.items[3].score == 0
    assert parse.await_count == 2


@pytest.mark.asyncio
async def test_sdk_parse_validation_feedback_does_not_expose_input():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)

    async def reply(**arguments):
        payload = json.loads(arguments["input"][1]["content"])
        raw = selected_result(good, payload)
        raw["items"][3]["score"] = "private-input-do-not-display"
        # 실제 SDK가 내부 Pydantic 파싱 단계에서 거절하는 경로를 재현한다.
        return arguments["text_format"].model_validate(raw)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context, business_call=fixture.business, rag_call=fixture.rag,
        max_validation_retries=0,
    )
    with pytest.raises(EvaluationError) as caught:
        await agent.evaluate_collected(fixture.request, evidence=evidence, materials=materials)
    assert "items.3: 점수는 정수" in str(caught.value)
    assert "private-input" not in str(caught.value)
    assert parse.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["incomplete", "refused", "missing", "api_error"])
async def test_incomplete_refused_missing_and_api_error_are_safe(case):
    fixture = ToolFixture()
    if case == "api_error":
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401,
                    json={"error": {"message": "SECRET-ERROR-BODY", "type": "auth_error"}},
                )
            )
        ) as http_client:
            async with AsyncOpenAI(
                api_key="fake", http_client=http_client, max_retries=0
            ) as client:
                agent = OpenAIEvaluator(
                    client,
                    context=fixture.context,
                    business_call=fixture.business,
                    rag_call=fixture.rag,
                )
                with pytest.raises(EvaluationError) as caught:
                    await agent.evaluate(fixture.request)
        assert "401" in str(caught.value)
        assert "SECRET-ERROR-BODY" not in str(caught.value)
        return
    parse = AsyncMock(
        return_value=SimpleNamespace(
            status="incomplete" if case == "incomplete" else "completed",
            output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])]
            if case == "refused"
            else [],
            output_parsed=None,
        )
    )
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context,
        business_call=fixture.business,
        rag_call=fixture.rag,
    )
    with pytest.raises(EvaluationError):
        await agent.evaluate(fixture.request)


@pytest.mark.asyncio
async def test_changed_approved_policy_fails_before_llm():
    fixture = ToolFixture()
    original = fixture.rag

    async def changed(name, arguments):
        env = await original(name, arguments)
        if "chunk_id" in arguments:
            env["data"]["text"] = "승인 이후 원문 변경"
        return env

    parse = AsyncMock()
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context,
        business_call=fixture.business,
        rag_call=changed,
    )
    with pytest.raises(ValueError, match="승인된 평가기준"):
        await agent.evaluate(fixture.request)
    parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_retrieved_reference_versions_fail_before_llm():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    approved_ids = {ref.document_id for ref in fixture.request.approved_criteria.references}
    ref = next(ref for ref in materials.references if ref.document_id not in approved_ids)
    materials.references.append(ref.model_copy(update={"content": "같은 버전의 다른 본문"}))
    parse = AsyncMock()
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)), context=fixture.context,
        business_call=fixture.business, rag_call=fixture.rag,
    )
    with pytest.raises(EvaluationError, match="같은 문서 버전·구간"):
        await agent.evaluate_collected(fixture.request, evidence=evidence, materials=materials)
    parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_access_and_obsolete_reference_are_not_used():
    fixture = ToolFixture()
    fixture.raw["reference_access_log"].append(
        {
            "doc_id": "FUTURE",
            "version": "v1",
            "accessed_at": "2099-01-01T00:00:00Z",
        }
    )
    fixture.reference_status = "폐기"
    evidence, materials = await collect(fixture)
    assert not any(record["doc_id"] == "FUTURE" for record in materials.details["accesses"])
    assert {(ref.document_id, ref.version) for ref in materials.references} == {("P1", "v1")}
    assert any(value.startswith("non_current_reference:") for value in materials.missing_sources)


@pytest.mark.asyncio
async def test_no_calls_after_approval_changes():
    fixture = ToolFixture()
    fixture.request.approved_criteria.items[0].title = "변경됨"
    parse = AsyncMock()
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context,
        business_call=fixture.business,
        rag_call=fixture.rag,
    )
    with pytest.raises(ValueError):
        await agent.evaluate(fixture.request)
    assert fixture.calls == []
    parse.assert_not_awaited()


def test_cli_help_and_no_keys(monkeypatch, capsys):
    from ai_agents.cli import evaluate as evaluate_cli

    monkeypatch.setattr("sys.argv", ["evaluate_cli", "--help"])
    with pytest.raises(SystemExit) as caught:
        evaluate_cli.main()
    assert caught.value.code == 0
    assert "--run-context" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_conversation_pagination_collects_all_51_messages():
    fixture = ToolFixture()
    fixture.raw["messages"] = [
        {
            "seq": i,
            "message_id": f"TEST-M{i:03d}",
            "role": "user" if i == 1 else "assistant",
            "content": f"메시지 {i}",
            "created_at": fixture.context.target.saved_at.isoformat(),
        }
        for i in range(1, 52)
    ]
    fixture.context.snapshot["conversation_range"] = {"from_seq": 1, "to_seq": 51}
    for env in [fixture.responses["work_case_response"], *fixture.responses["artifact_responses"]]:
        env["meta"]["snapshot"] = deepcopy(fixture.context.snapshot)
    _, materials = await collect(fixture)
    assert len(materials.details["messages"]) == 51
    assert sum(name == "get_conversation" for _, name, _ in fixture.calls) == 2


@pytest.mark.asyncio
async def test_only_retryable_temporary_mcp_failure_is_retried_once():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)
    attempts = 0

    async def flaky(name, arguments):
        nonlocal attempts
        if name == "get_work_case":
            attempts += 1
            if attempts == 1:
                return {
                    "ok": False,
                    "error": {
                        "code": "TEMPORARY_FAILURE",
                        "message": "한 번만 실패",
                        "retryable": True,
                    },
                }
        return await fixture.business(name, arguments)

    async def reply(**arguments):
        payload = json.loads(arguments["input"][1]["content"])
        parsed = arguments["text_format"].model_validate(selected_result(good, payload))
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        context=fixture.context,
        business_call=flaky,
        rag_call=fixture.rag,
    )
    await agent.evaluate(fixture.request)
    assert attempts == 2
    assert parse.await_count == 1


@pytest.mark.asyncio
async def test_security_violation_cannot_receive_two_points():
    from ai_agents.shared.evidence_models import PolicyViolation

    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    evidence.policy_check.violations = [
        PolicyViolation(
            rule_id="V-PII-INPUT",
            clause="ART-07",
            location=evidence.policy_check.scope.user_message_ids[0],
        )
    ]
    materials.evidence_sha256 = content_hash(evidence)
    result = valid_result(fixture, evidence, materials)
    result.items[3].score = 2
    with pytest.raises(EvaluationError, match="정책 위반"):
        validate_evaluation_result(fixture.request, result, evidence=evidence, materials=materials)


@pytest.mark.asyncio
async def test_security_wrong_source_gets_policy_choices_in_bounded_retry():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    good = valid_result(fixture, evidence, materials)
    calls = 0

    async def reply(**arguments):
        nonlocal calls
        calls += 1
        payload = json.loads(arguments["input"][1]["content"])
        assert payload["security_evidence"]["available"] is True
        ids = payload["security_evidence"]["policy_check_choice_ids"]
        assert ids
        raw = selected_result(good, payload)
        if calls == 1:
            wrong = next(
                row["id"] for row in payload["work_evidence_catalog"]
                if row["kind"] == "message:user"
            )
            raw["items"][3]["required_work_evidence_id"] = wrong
        else:
            feedback = arguments["input"][-1]["content"]
            assert "보안 점수에는 서버 정책 검사 근거가 필요합니다" in feedback
            assert '"eligible_required_work_ids"' in feedback
            assert ids[0] in feedback
        parsed = arguments["text_format"].model_validate(raw)
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)), context=fixture.context,
        business_call=fixture.business, rag_call=fixture.rag,
    )
    result = await agent.evaluate_collected(fixture.request, evidence=evidence, materials=materials)
    assert parse.await_count == 2
    security = result.items[3]
    assert {proof.source_id for proof in security.work_evidence} == {
        evidence.policy_check.evidence_id,
    }


@pytest.mark.asyncio
async def test_missing_policy_is_explicit_in_model_input_and_returns_no_security_score():
    fixture = ToolFixture()
    evidence, materials = await collect(fixture)
    public = valid_result(fixture, evidence, materials)
    public.items[3].observation = "insufficient_data"
    public.items[3].score = None
    public.items[3].work_evidence = []
    evidence.policy_check = None
    materials.evidence_sha256 = content_hash(evidence)

    async def reply(**arguments):
        payload = json.loads(arguments["input"][1]["content"])
        assert payload["security_evidence"] == {
            "available": False, "policy_check_choice_ids": [], "violations_detected": None,
        }
        assert "policy_check" in payload["missing_sources"]
        parsed = arguments["text_format"].model_validate(selected_result(public, payload))
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    parse = AsyncMock(side_effect=reply)
    agent = OpenAIEvaluator(
        SimpleNamespace(responses=SimpleNamespace(parse=parse)), context=fixture.context,
        business_call=fixture.business, rag_call=fixture.rag,
    )
    checked = await agent.evaluate_collected(
        fixture.request, evidence=evidence, materials=materials,
    )
    assert checked.items[3].score is None
    assert checked.items[3].observation == "insufficient_data"
    assert parse.await_count == 1


def test_cli_missing_configuration_has_actionable_error(monkeypatch, capsys, tmp_path):
    from ai_agents.cli import evaluate as evaluate_cli

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RUN_TOKEN", raising=False)
    monkeypatch.delenv("MCP_RUN_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(evaluate_cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        "sys.argv", ["evaluate_cli", "--input", "unused.json", "--run-context", "unused.json"]
    )
    assert evaluate_cli.main() == 2
    assert "OPENAI_API_KEY와 RUN_TOKEN" in capsys.readouterr().err
