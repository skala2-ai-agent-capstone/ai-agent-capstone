import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_agents.evaluation_agent.final.models import AgentEvaluationOutput, AgentItemEvaluation
from ai_agents.evaluation_agent.final.payload import (
    build_evaluation_payload,
    prepare_mcp_evaluation_payload,
)
from ai_agents.integrations.mcp_adapter import (
    MCPToolError,
    assemble_evaluation_evidence,
    decode_mcp_result,
    parse_policy_check_response,
    parse_work_case_response,
    read_mcp_response,
)
from ai_agents.shared.evidence_models import EvaluationRun
from ai_agents.shared.models import ClassificationResult, WorkContext
from ai_agents.shared.workflow_models import (
    Approval,
    CriteriaDraft,
    EvaluationInput,
    content_hash,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    json.loads(line)
    for line in (ROOT / "mcp_servers/data/all_cases.jsonl").read_text().splitlines()
    if line.strip()
]


def response(context, data):
    return {
        "ok": True,
        "data": deepcopy(data),
        "meta": {
            "run_id": context.run_id,
            "snapshot": deepcopy(context.snapshot),
        },
    }


def inputs(case):
    """실제 raw 자료를 서버 응답 형식으로 투영. DB·네트워크·LLM은 사용하지 않는다."""
    raw = case["raw"]
    work = raw["work_case"]
    target = max(raw["artifact_versions"], key=lambda item: item["saved_at"])
    versions = [v for v in raw["artifact_versions"] if v["saved_at"] <= target["saved_at"]]
    seqs = [m["seq"] for m in raw["messages"] if m["created_at"] <= target["saved_at"]]
    context = EvaluationRun(
        run_id="RUN-TEST",
        work_id=work["work_id"],
        snapshot={
            "artifact_ref": {k: target[k] for k in ("artifact_id", "version", "saved_at")},
            "reference_refs": [
                {k: r[k] for k in ("doc_id", "version")} for r in raw["attached_references"]
            ],
            "linked_doc_refs": [],
            "conversation_range": {
                "from_seq": min(seqs) if seqs else 0,
                "to_seq": max(seqs) if seqs else 0,
            },
        },
    )
    artifacts = [
        {
            **{k: v[k] for k in ("artifact_id", "version", "saved_at", "author", "content")},
            "work_id": work["work_id"],
            "is_evaluation_target": v["version"] == target["version"],
            "spans": [{k: span[k] for k in ("span_id", "text")} for span in v["spans"]],
        }
        for v in versions
    ]
    source = work.get("source_message")
    message = (
        None
        if source is None
        else {
            "evidence_id": f"{work['work_id']}:source_message",
            "channel": source.get("channel"),
            "received_at": source.get("received_at"),
            "content": source["content"],
            "content_type": "data",
            "lines": [
                {"line_id": f"{work['work_id']}:source_message:L{i:02d}", "text": text}
                for i, text in enumerate(source["content"].split("\n"), 1)
            ],
        }
    )
    work_data = {
        "work_id": work["work_id"],
        "registration": work.get("registration"),
        "registration_evidence_id": (
            f"{work['work_id']}:registration" if work.get("registration") is not None else None
        ),
        "source_message": message,
        "artifact_refs": [
            {
                k: a[k]
                for k in (
                    "artifact_id",
                    "version",
                    "saved_at",
                    "author",
                    "is_evaluation_target",
                )
            }
            for a in artifacts
        ],
        "work_type": "THIS_LABEL_MUST_NOT_BE_COPIED",
    }
    return {
        "context": context,
        "work_case_response": response(context, work_data),
        "artifact_responses": [response(context, artifact) for artifact in artifacts],
    }


@pytest.fixture
def responses():
    return inputs(CASES[0])


def policy_response(responses):
    context = responses["context"]
    target = next(
        r["data"] for r in responses["artifact_responses"] if r["data"]["is_evaluation_target"]
    )
    return response(
        context,
        {
            "work_id": context.work_id,
            "version": target["version"],
            "evidence_id": f"{context.work_id}:policy_check",
            "checked_at": target["saved_at"],
            "scope": {
                "user_message_ids": ["USER-MSG-1"],
                "artifact_ref": {k: target[k] for k in ("artifact_id", "version", "author")},
                "note": "테스트용 서버 검사 결과",
            },
            "violations": [],
            "masking": [],
        },
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case_id"])
def test_all_40_real_cases_keep_original_ids_and_versions(case):
    data = inputs(case)
    evidence = assemble_evaluation_evidence(**data)
    assert evidence.context.work_id == case["raw"]["work_case"]["work_id"]
    assert evidence.missing_sources == ["policy_check"]
    assert "work_type" not in evidence.work_case.model_dump()
    for artifact in evidence.artifacts:
        original = next(
            v for v in case["raw"]["artifact_versions"] if v["version"] == artifact.version
        )
        assert artifact.content == original["content"]
        for span in original["spans"]:
            assert evidence.evidence_sources()[span["span_id"]] == span["text"]


@pytest.mark.parametrize("registration", [None, "", {}, {"customer_purpose": "  원문  "}])
def test_absent_empty_and_present_registration_are_distinct(responses, registration):
    work = responses["work_case_response"]["data"]
    work["registration"] = registration
    work["registration_evidence_id"] = (
        f"{work['work_id']}:registration" if registration is not None else None
    )
    evidence = assemble_evaluation_evidence(**responses)
    record = evidence.work_case.registration
    assert (record is None) == (registration is None)
    if record is not None:
        assert record.content == registration


@pytest.mark.parametrize("field", ["registration", "registration_evidence_id", "source_message"])
def test_missing_work_fields_are_not_interpreted_as_empty(responses, field):
    del responses["work_case_response"]["data"][field]
    with pytest.raises(ValueError, match="必須|필수"):
        assemble_evaluation_evidence(**responses)


@pytest.mark.parametrize("changed", ["run_id", "version", "reference_refs", "conversation_range"])
def test_other_run_or_snapshot_is_rejected(responses, changed):
    env = responses["artifact_responses"][0]
    if changed == "run_id":
        env["meta"]["run_id"] = "OTHER-RUN"
    elif changed == "version":
        env["meta"]["snapshot"]["artifact_ref"]["version"] = "v99"
    elif changed == "reference_refs":
        env["meta"]["snapshot"][changed] = [{"doc_id": "OTHER", "version": "v1"}]
    else:
        env["meta"]["snapshot"][changed]["to_seq"] += 1
    with pytest.raises(ValueError):
        assemble_evaluation_evidence(**responses)


@pytest.mark.parametrize("change", ["foreign_work", "future", "duplicate", "missing", "span_id"])
def test_invalid_artifact_collection_is_rejected(responses, change):
    artifacts = responses["artifact_responses"]
    if change == "foreign_work":
        artifacts[0]["data"]["work_id"] = "OTHER-WORK"
    elif change == "future":
        artifacts[0]["data"]["saved_at"] = "2099-01-01T00:00:00Z"
    elif change == "duplicate":
        artifacts.append(deepcopy(artifacts[0]))
    elif change == "missing":
        artifacts.pop(0)
    else:
        artifacts[0]["data"]["spans"][0]["span_id"] = "FOREIGN:v99:L01"
    with pytest.raises(ValueError):
        assemble_evaluation_evidence(**responses)


def test_policy_result_and_meta_are_preserved(responses):
    env = policy_response(responses)
    result, meta = parse_policy_check_response(env, context=responses["context"])
    assert result.evidence_id == env["data"]["evidence_id"]
    assert meta.snapshot == responses["context"].snapshot
    evidence = assemble_evaluation_evidence(**responses, policy_check_response=env)
    assert evidence.missing_sources == []
    assert result.evidence_id in evidence.evidence_sources()


@pytest.mark.parametrize("change", ["version", "work_id", "missing_violations", "location"])
def test_invalid_policy_result_is_rejected(responses, change):
    env = policy_response(responses)
    if change in {"version", "work_id"}:
        env["data"][change] = "OTHER"
    elif change == "missing_violations":
        del env["data"]["violations"]
    else:
        env["data"]["violations"] = [
            {
                "rule_id": "V1",
                "clause": "ART-07",
                "location": "FOREIGN-MESSAGE",
            }
        ]
    with pytest.raises(ValueError):
        assemble_evaluation_evidence(**responses, policy_check_response=env)


def test_customer_lines_keep_whitespace_and_cannot_be_replaced(responses):
    work = responses["work_case_response"]["data"]
    prefix = f"{work['work_id']}:source_message"
    work["source_message"] = {
        "evidence_id": prefix,
        "channel": None,
        "received_at": None,
        "content_type": "data",
        "content": "  고객 원문  \n",
        "lines": [
            {"line_id": f"{prefix}:L01", "text": "  고객 원문  "},
            {"line_id": f"{prefix}:L02", "text": ""},
        ],
    }
    result, _ = parse_work_case_response(
        responses["work_case_response"],
        context=responses["context"],
    )
    assert result.source_message.content == "  고객 원문  \n"
    work["source_message"]["lines"][0]["text"] = "변조"
    with pytest.raises(ValidationError):
        assemble_evaluation_evidence(**responses)


def test_mcp_failure_keeps_code_retryable_and_meta():
    with pytest.raises(MCPToolError) as caught:
        read_mcp_response(
            {
                "ok": False,
                "error": {
                    "code": "TEMPORARY_FAILURE",
                    "message": "잠시 후 재시도",
                    "retryable": True,
                },
                "meta": {"run_id": "RUN"},
            }
        )
    assert caught.value.code == "TEMPORARY_FAILURE"
    assert caught.value.retryable is True
    assert caught.value.meta == {"run_id": "RUN"}


@pytest.mark.parametrize("kind", ["dict", "string", "wrapped", "snake", "camel", "text"])
def test_sdk_json_and_structured_results(responses, kind):
    env = responses["work_case_response"]
    variants = {
        "dict": env,
        "string": json.dumps(env),
        "wrapped": {"result": env},
        "snake": SimpleNamespace(structured_content=env),
        "camel": SimpleNamespace(structuredContent=env),
        "text": SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(env))]),
    }
    assert decode_mcp_result(variants[kind]) == env


@pytest.mark.parametrize("result", ["not json", "[]", {"ok": "true"}, {"ok": 1}])
def test_malformed_responses_are_rejected(result):
    with pytest.raises(ValueError):
        read_mcp_response(decode_mcp_result(result))


def approved_request(context):
    # 분류·승인 모듈의 확정 분류와 사람의 승인을 테스트에서만 대체한다.
    work = WorkContext(
        work_id=context.work_id,
        messages=[
            {"id": "m1", "role": "user", "content": "고객 맞춤 제안서를 작성해줘."},
        ],
        coverage="partial",
        missing_sources=["추가 참조 자료"],
    )
    classification = ClassificationResult(
        status="classified",
        work_type="proposal",
        reason="테스트 분류",
        evidence=[{"source_id": "m1", "quote": "제안서"}],
        candidate_types=["proposal"],
        missing_information=[],
    )
    rule = {"document_id": "P1", "version": "v1", "section_id": "S1", "quote": "기준 원문"}
    criteria = CriteriaDraft(
        work_type="proposal",
        references=[
            {
                "document_id": "P1",
                "version": "v1",
                "section_id": "S1",
                "content": "기준 원문",
            }
        ],
        items=[
            {
                "id": f"proposal.{key}",
                "title": key,
                "dimension": "employee_behavior",
                "levels": [{"score": 0, "condition": "미충족"}, {"score": 2, "condition": "충족"}],
                "unobserved_condition": "기록 없음",
                "rule_evidence": [rule],
            }
            for key in [
                "problem_definition",
                "instruction_quality",
                "verification",
                "security",
                "quality",
            ]
        ],
    )
    return EvaluationInput(
        run_id=context.run_id,
        work_record=work,
        classification=classification,
        approved_criteria=criteria,
        approval=Approval(
            status="approved",
            run_id=context.run_id,
            work_sha256=content_hash(work),
            criteria_sha256=content_hash(criteria),
            revision=1,
            approved_at=datetime.now(timezone.utc),
        ),
    )


def test_evidence_is_connected_to_approved_payload(responses):
    evidence = assemble_evaluation_evidence(**responses)
    request = approved_request(responses["context"])
    payload = build_evaluation_payload(request, evidence=evidence)
    assert payload["evidence_sha256"] == content_hash(evidence)
    assert payload["criteria_sha256"] == request.approval.criteria_sha256
    assert payload["work_evidence_sources"]["m1"] == request.work_record.messages[0].content
    assert "policy_check" in payload["missing_sources"]
    assert build_evaluation_payload(request)["criteria"] == payload["criteria"]
    request.run_id = "OTHER"
    request.approval.run_id = "OTHER"
    with pytest.raises(ValueError, match="실행 또는 업무"):
        build_evaluation_payload(request, evidence=evidence)


@pytest.mark.asyncio
async def test_read_tools_are_connected_and_no_write_or_llm_is_called(responses):
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        if name == "get_work_case":
            return json.dumps(responses["work_case_response"])
        if name == "get_artifact_version":
            return next(
                r
                for r in responses["artifact_responses"]
                if (r["data"]["artifact_id"], r["data"]["version"])
                == (arguments["artifact_id"], arguments["version"])
            )
        assert name == "get_policy_check_result"
        return policy_response(responses)

    request = approved_request(responses["context"])
    payload = await prepare_mcp_evaluation_payload(
        request,
        context=responses["context"],
        call_tool=call_tool,
    )
    assert payload["missing_sources"] == ["추가 참조 자료"]
    assert len(calls) == len(responses["artifact_responses"]) + 2
    assert all("company_id" not in args and "user_id" not in args for _, args in calls)
    calls.clear()
    request.approved_criteria.items[0].title = "승인 이후 변경"
    with pytest.raises(ValidationError, match="승인 이후 평가기준"):
        await prepare_mcp_evaluation_payload(
            request,
            context=responses["context"],
            call_tool=call_tool,
        )
    assert calls == []


def item_result(**changes):
    return {
        "criterion_id": "proposal.quality",
        "observation": "observed",
        "score": 2,
        "reason": "테스트",
        "work_evidence": [{"source_id": "m1", "quote": "원문"}],
        "rule_evidence": [
            {"document_id": "P1", "version": "v1", "section_id": "S1", "quote": "기준"}
        ],
        "review_required": False,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"score": 3},
        {"score": -1},
        {"score": True},
        {"score": "2"},
        {"criterion_id": "proposal.security", "score": 1},
        {"observation": "unobserved"},
        {"work_evidence": []},
        {"observation": "needs_review", "score": None},
    ],
)
def test_evaluation_result_constraints(changes):
    with pytest.raises(ValidationError):
        AgentItemEvaluation.model_validate(item_result(**changes))


@pytest.mark.parametrize("count", [4, 5, 6])
def test_output_requires_five_unique_items(count):
    data = {
        "run_id": "RUN",
        "criteria_sha256": "hash",
        "summary": "결과",
        "coaching": [],
        "items": [item_result(criterion_id="proposal.quality") for _ in range(count)],
    }
    with pytest.raises(ValidationError):
        AgentEvaluationOutput.model_validate(data)
