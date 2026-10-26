import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_complete_workflow import UnifiedGateway, client_fixture

from ai_agents.evaluation_agent.final.stored_rubric import StoredRubric
from ai_agents.evaluation_agent.final.validation import EvaluationError
from ai_agents.integrations.final_mcp import FinalMCPGateway
from ai_agents.integrations.mcp_client import MCPError, MCPGateway
from ai_agents.orchestration.final_evaluation import (
    FinalCheckpoint,
    FinalEvaluationJob,
    FinalEvaluationWorkflow,
    PreparedFinalEvaluation,
)

ROOT = Path(__file__).resolve().parents[1]
JOB = FinalEvaluationJob(evaluation_id="EVAL-TEST", operation_id="final-v1")


class StoredGateway(UnifiedGateway):
    def __init__(self, work_type="proposal"):
        super().__init__(work_type)
        docs = json.loads((ROOT / "mcp_servers/data/seed/documents.json").read_text())
        doc = next(
            d for d in docs if d.get("collection") == "rubrics" and d["work_type"] == work_type
        )
        self.rubric = {k: doc[k] for k in ("doc_id", "version", "work_type", "status", "items")}
        self.rubric["content_type"] = "data"
        self.fixture.responses["work_case_response"]["data"]["work_type"] = work_type
        target = self.fixture.context.target
        self.target = {
            "evaluation_id": JOB.evaluation_id,
            "work_id": self.fixture.context.work_id,
            "artifact_id": target.artifact_id,
            "artifact_version": target.version,
            "rubric_version": doc["version"],
            "status": "대기",
            "draft_revision": None,
            "coaching_revision": None,
        }
        self.saved = {}
        self.fail_coaching = False
        self.lose_draft_response = False

    def envelope(self, data):
        return {
            "ok": True,
            "data": deepcopy(data),
            "meta": {
                "run_id": self.fixture.context.run_id,
                "snapshot": self.fixture.context.snapshot,
            },
        }

    async def call(self, server, tool, arguments):
        if tool not in {
            "get_evaluation_context",
            "search_rubric",
            "save_evaluation_draft",
            "save_coaching",
        }:
            return await super().call(server, tool, arguments)
        self.calls.append((server, tool, deepcopy(arguments)))
        if tool == "get_evaluation_context":
            return self.envelope(self.target)
        if tool == "search_rubric":
            return self.envelope(self.rubric)
        if tool == "save_coaching" and self.fail_coaching:
            raise MCPError("TRANSPORT_ERROR", tool)
        key = (tool, arguments["idempotency_key"])
        if key not in self.saved:
            revision = "draft_revision" if tool == "save_evaluation_draft" else "coaching_revision"
            self.saved[key] = {
                "evaluation_id": JOB.evaluation_id,
                revision: 1,
                "items_saved": 5,
                "saved_at": "2026-10-23T00:00:00+09:00",
            }
            self.target[revision] = 1
        if tool == "save_evaluation_draft" and self.lose_draft_response:
            self.lose_draft_response = False
            raise MCPError("TRANSPORT_ERROR", tool)
        return self.envelope(self.saved[key])


def workflow(gateway, tmp_path, client=None):
    client = client or client_fixture()
    return FinalEvaluationWorkflow(
        gateway,
        client,
        model="test-model",
        checkpoint_dir=tmp_path,
        sleep=AsyncMock(),
        api_mode="responses",
    ), client


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_stored_criteria_to_final_draft_and_coaching(work_type, tmp_path):
    gateway = StoredGateway(work_type)
    original = deepcopy(gateway.rubric)
    run, client = workflow(gateway, tmp_path)
    result = await run.run(JOB)
    assert result["status"] == "draft_and_coaching_saved"
    assert gateway.rubric == original
    assert client.responses.parse.await_count == 1
    assert [t for _, t, _ in gateway.calls if t.startswith("save_")] == [
        "save_evaluation_draft",
        "save_coaching",
    ]
    assert [a for _, t, a in gateway.calls if t == "search_rubric"] == [
        {
            "work_type": work_type,
            "rubric_version": original["version"],
        }
    ]
    draft = next(a for _, t, a in gateway.calls if t == "save_evaluation_draft")
    assert {i["item_id"] for i in draft["items"]} == {i["item_id"] for i in original["items"]}
    assert all(i["status"] == "채점" and i["evidence_ids"] for i in draft["items"])
    coaching = next(a for _, t, a in gateway.calls if t == "save_coaching")
    # 채점된 항목의 코칭은 근거 구간을 대상으로 하는 보완점이다.
    assert all(
        c["kind"] == "보완점" and c["target_span_id"] for c in coaching["coaching"]
    )
    assert not any(
        t in {"save_rubric", "get_current_rubric", "finalize"} for _, t, _ in gateway.calls
    )
    prepared = PreparedFinalEvaluation.model_validate_json(Path(result["checkpoint"]).read_text())
    assert "approval" not in prepared.request.model_dump()
    assert (
        prepared.request.stored_rubric.model_dump()
        == StoredRubric.model_validate(original).model_dump()
    )
    assert Path(result["checkpoint"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_resume_after_coaching_failure_never_regenerates_or_resaves_draft(tmp_path):
    gateway = StoredGateway()
    gateway.fail_coaching = True
    run, client = workflow(gateway, tmp_path)
    with pytest.raises(MCPError):
        await run.run(JOB)
    assert client.responses.parse.await_count == 1
    gateway.fail_coaching = False
    restart, new_client = workflow(gateway, tmp_path)
    result = await restart.run(JOB)
    assert result["status"] == "draft_and_coaching_saved"
    new_client.responses.parse.assert_not_awaited()
    assert len([t for _, t, _ in gateway.calls if t == "save_evaluation_draft"]) == 1
    calls = deepcopy(gateway.calls)
    assert await restart.run(JOB) == result
    assert [t for _, t, _ in gateway.calls[len(calls) :]] == ["get_evaluation_context"]


@pytest.mark.asyncio
async def test_ambiguous_save_response_retries_identical_payload(tmp_path):
    gateway = StoredGateway()
    gateway.lose_draft_response = True
    run, client = workflow(gateway, tmp_path)
    await run.run(JOB)
    drafts = [a for _, t, a in gateway.calls if t == "save_evaluation_draft"]
    assert len(drafts) == 2 and drafts[0] == drafts[1]
    assert len(gateway.saved) == 2
    assert client.responses.parse.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "wrong_version",
        "wrong_type",
        "draft_rubric",
        "duplicate",
        "missing_type",
        "terminal",
        "wrong_artifact",
    ],
)
async def test_invalid_context_or_rubric_stops_before_llm_and_writes(tmp_path, change):
    gateway = StoredGateway()
    if change == "wrong_version":
        gateway.rubric["version"] = "v99"
    elif change == "wrong_type":
        gateway.rubric["work_type"] = "quote"
    elif change == "draft_rubric":
        gateway.rubric["status"] = "초안"
    elif change == "duplicate":
        gateway.rubric["items"][1] = deepcopy(gateway.rubric["items"][0])
    elif change == "missing_type":
        gateway.fixture.responses["work_case_response"]["data"]["work_type"] = None
    elif change == "terminal":
        gateway.target["status"] = "확정"
    else:
        gateway.target["artifact_version"] = "v999"
    run, client = workflow(gateway, tmp_path)
    with pytest.raises((EvaluationError, ValueError, ValidationError)):
        await run.run(JOB)
    client.responses.parse.assert_not_awaited()
    assert not gateway.saved


@pytest.mark.asyncio
async def test_pinned_deprecated_rubric_is_not_replaced_by_latest(tmp_path):
    gateway = StoredGateway()
    gateway.rubric["status"] = "폐기"
    run, _ = workflow(gateway, tmp_path)
    result = await run.run(JOB)
    assert result["rubric_version"] == gateway.target["rubric_version"]


@pytest.mark.asyncio
async def test_bad_model_evidence_is_not_saved(tmp_path):
    gateway = StoredGateway()
    client = client_fixture()
    original = client.responses.parse.side_effect

    async def corrupt(**kwargs):
        reply = await original(**kwargs)
        raw = reply.output_parsed.model_dump(mode="json")
        raw["items"][0]["work_evidence_ids"] = ["unknown"]
        reply.output_parsed = SimpleNamespace(
            model_dump=lambda **_: raw,
            model_dump_json=lambda: json.dumps(raw),
        )
        return reply

    client.responses.parse.side_effect = corrupt
    run, _ = workflow(gateway, tmp_path, client)
    with pytest.raises((EvaluationError, ValueError, KeyError)):
        await run.run(JOB)
    assert not gateway.saved


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("rubric_version", "v999"), ("draft_revision", 2)])
async def test_resumed_scope_change_is_rejected(tmp_path, field, value):
    gateway = StoredGateway()
    run, _ = workflow(gateway, tmp_path)
    await run.run(JOB)
    gateway.target[field] = value
    with pytest.raises(EvaluationError, match="체크포인트"):
        await run.run(JOB)


@pytest.mark.asyncio
async def test_existing_server_draft_without_checkpoint_does_not_regenerate(tmp_path):
    gateway = StoredGateway()
    gateway.target["draft_revision"] = 1
    run, client = workflow(gateway, tmp_path)
    with pytest.raises(EvaluationError, match="체크포인트"):
        await run.run(JOB)
    client.responses.parse.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation,status",
    [
        ("unobserved", "미관측"),
        ("needs_review", "검토 필요"),
        ("insufficient_data", "검토 필요"),
    ],
)
async def test_unscored_items_keep_null_score_and_record_guidance(tmp_path, observation, status):
    gateway, client = StoredGateway(), client_fixture()
    original = client.responses.parse.side_effect

    async def unscored(**kwargs):
        reply = await original(**kwargs)
        raw = reply.output_parsed.model_dump(mode="json")
        requirements = json.loads(kwargs["input"][1]["content"])["criterion_requirements"]
        for row in raw["items"]:
            # 검토 필요는 상충·모호함을 보여 주는 업무 근거를 함께 고른다.
            review_work = requirements[row["criterion_id"]]["required_work_choice_ids"][0]
            row.update(
                observation=observation,
                score=None,
                required_work_evidence_id=review_work if observation == "needs_review" else None,
                work_evidence_ids=[],
                review_required=observation != "unobserved",
            )
        reply.output_parsed = kwargs["text_format"].model_validate(raw)
        return reply

    client.responses.parse.side_effect = unscored
    run, _ = workflow(gateway, tmp_path, client)
    await run.run(JOB)
    draft = next(a for _, t, a in gateway.calls if t == "save_evaluation_draft")
    coaching = next(a for _, t, a in gateway.calls if t == "save_coaching")
    assert all(i["status"] == status and i["score"] is None for i in draft["items"])
    reviewed = observation == "needs_review"
    assert all(bool(i["evidence_ids"]) == reviewed for i in draft["items"])
    # 검토 필요는 근거를 남기지만 코칭은 점수 없는 항목과 같이 기록 안내다.
    assert all(
        c["kind"] == "기록 안내" and c["target_span_id"] is None for c in coaching["coaching"]
    )


@pytest.mark.asyncio
async def test_chat_completion_adapter_uses_schema_and_proxy_headers(tmp_path):
    gateway = StoredGateway()
    reference = client_fixture()

    async def chat(**kwargs):
        # 기존 선택 스키마를 만드는 평가기에 Responses 대역을 연결해 유효 출력 생성.
        from ai_agents.evaluation_agent.final.selection import selection_model
        from ai_agents.orchestration.final_evaluation import PreparedFinalEvaluation

        state = PreparedFinalEvaluation.model_validate_json(
            next((tmp_path / "baseline").glob("*.json")).read_text()
        )
        from ai_agents.evaluation_agent.final.selection import build_choices
        from ai_agents.evaluation_agent.final.validation import evidence_catalog, reference_catalog

        sources, kinds = evidence_catalog(state.request, state.evidence, state.materials)
        choices = build_choices(
            state.request,
            sources,
            kinds,
            reference_catalog(state.request, state.materials),
            evidence=state.evidence,
        )
        result = await reference.responses.parse(
            input=kwargs["messages"],
            text_format=selection_model(choices, state.request),
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=result.output_parsed.model_dump_json()),
                )
            ]
        )

    baseline, _ = workflow(gateway, tmp_path / "baseline")
    await baseline.run(JOB)
    gateway = StoredGateway()
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=chat),
            )
        )
    )
    run = FinalEvaluationWorkflow(
        gateway, client, model="test-model", checkpoint_dir=tmp_path / "chat"
    )
    await run.run(JOB)
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["extra_headers"] == {
        "X-Stage": "evaluate",
        "X-Work-Id": gateway.fixture.context.work_id,
    }


def test_checkpoint_lock_rejects_concurrent_job(tmp_path):
    checkpoint = FinalCheckpoint(tmp_path, JOB)
    with checkpoint.locked(), pytest.raises(EvaluationError, match="이미 진행"):
        with FinalCheckpoint(tmp_path, JOB).locked():
            pass


@pytest.mark.asyncio
async def test_gateway_keeps_readonly_client_readonly_and_blocks_unrelated_writes():
    read = MCPGateway("test-token", business_url="unused", rag_url="unused")
    final = FinalMCPGateway("test-token", business_url="unused", rag_url="unused")
    final._call = AsyncMock(return_value={"ok": True})
    with pytest.raises(MCPError):
        await read.call("business", "save_evaluation_draft", {})
    for name in ("save_rubric", "request_review", "finalize", "update_progress"):
        with pytest.raises(MCPError):
            await final.call("business", name, {})
    await final.call("business", "get_evaluation_context", {})
    await final.call("business", "save_evaluation_draft", {})
    assert final._call.await_count == 2
