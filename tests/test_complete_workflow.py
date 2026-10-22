import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command
from test_evaluation_agent import ToolFixture, selected_result
from test_mcp_adapter import ROOT

from ai_agents.classification.agent import ModelReply
from ai_agents.evaluation_agent.final.handoff import HandoffEvaluator
from ai_agents.evaluation_agent.final.models import AgentEvaluationOutput
from ai_agents.evaluation_agent.final.workflow import build_evaluation_workflow
from ai_agents.orchestration.graph import MCPHandoff
from ai_agents.shared.models import ClassificationResult
from ai_agents.shared.workflow_models import CriteriaDraft


class UnifiedGateway:
    """실제 업무 자료와 MCP 전송 대역으로 분류·승인 및 평가 모듈의 연결 경계를 함께 검증한다."""

    def __init__(self, work_type="proposal"):
        self.fixture = ToolFixture(work_type)
        self.calls = []
        seeds = json.loads((ROOT / "mcp_servers/data/seed/documents.json").read_text())
        self.policy = next(doc for doc in seeds if doc.get("collection") == "policies")

    async def call(self, server, tool, arguments):
        self.calls.append((server, tool, deepcopy(arguments)))
        if tool == "get_linked_works":
            data = {"work_id": self.fixture.context.work_id, "works": []}
        elif tool == "search_policy":
            chunks = [
                {
                    **chunk,
                    "doc_id": self.policy["doc_id"],
                    "version": self.policy["version"],
                    "ref": f"{self.policy['doc_id']}@{self.policy['version']}#{chunk['chunk_id']}",
                }
                for chunk in self.policy["chunks"]
                if chunk["clause_id"] in arguments["clause_ids"]
                or chunk["chunk_id"] in arguments["clause_ids"]
            ]
            found = {chunk["clause_id"] for chunk in chunks} | {
                chunk["chunk_id"] for chunk in chunks
            }
            data = {
                "chunks": chunks,
                "no_evidence": not chunks,
                "missing_clause_ids": sorted(set(arguments["clause_ids"]) - found),
            }
        elif (
            tool == "get_reference"
            and arguments["doc_id"] == self.policy["doc_id"]
            and "chunk_id" not in arguments
        ):
            data = {
                **arguments,
                "status": "현행",
                "grade": "내부",
                "chunks": [
                    {
                        **c,
                        "ref": f"{self.policy['doc_id']}@{self.policy['version']}#{c['chunk_id']}",
                    }
                    for c in self.policy["chunks"]
                ],
            }
        elif tool == "get_reference" and arguments["doc_id"] == self.policy["doc_id"]:
            chunk = next(c for c in self.policy["chunks"] if c["chunk_id"] == arguments["chunk_id"])
            data = {
                **arguments,
                "text": chunk["text"],
                "ref": f"{arguments['doc_id']}@{arguments['version']}#{arguments['chunk_id']}",
            }
        elif server == "business":
            return await self.fixture.business(tool, arguments)
        else:
            return await self.fixture.rag(tool, arguments)
        return {
            "ok": True,
            "data": data,
            "meta": {
                "run_id": self.fixture.context.run_id,
                "snapshot": self.fixture.context.snapshot,
            },
        }


class Classifier:
    model = "test-classifier"

    def __init__(self, work_type):
        self.work_type = {"quote": "quotation", "inquiry_reply": "customer_response"}.get(
            work_type,
            work_type,
        )

    async def classify(self, *, system_prompt, payload):
        source = next(m for m in json.loads(payload)["messages"] if m["role"] == "user")
        return ModelReply(
            result=ClassificationResult(
                status="classified",
                work_type=self.work_type,
                reason="테스트의 고정 분류",
                evidence=[{"source_id": source["id"], "quote": source["content"][:20]}],
                candidate_types=[self.work_type],
                missing_information=[],
            ),
            response_id="test-classifier",
        )


class Builder:
    async def generate(self, work_type, policy, *, previous=None, feedback=None):
        chunk = policy.chunks[0]
        reference = {
            "document_id": chunk.doc_id,
            "version": chunk.version,
            "section_id": chunk.chunk_id,
            "content": chunk.text,
        }
        rule = {key: value for key, value in reference.items() if key != "content"}
        rule["quote"] = chunk.text
        return CriteriaDraft(
            work_type=work_type,
            references=[reference],
            items=[
                {
                    "id": f"{work_type}.{key}",
                    "title": f"{key} {feedback or ''}",
                    "dimension": "employee_behavior",
                    "levels": [
                        {"score": 0, "condition": "미충족"},
                        {"score": 2, "condition": "충족"},
                    ],
                    "unobserved_condition": "기록 없음",
                    "rule_evidence": [rule],
                }
                for key in (
                    "problem_definition",
                    "instruction_quality",
                    "verification",
                    "security",
                    "quality",
                )
            ],
        )


def client_fixture():
    async def parse(**arguments):
        payload = json.loads(arguments["input"][1]["content"])
        catalog = payload["work_evidence_catalog"]
        references = payload["reference_catalog"]
        items = []
        for criterion in payload["criteria"]["items"]:
            key = criterion["id"].rsplit(".", 1)[1]
            kind = {"quality": "target_artifact", "security": "policy_check"}.get(
                key, "message:user"
            )
            proof = next(row for row in catalog if row["kind"] == kind)
            rules = [criterion["rule_evidence"][0]]
            additional = next(
                (ref for ref in references if ref["document_id"] != rules[0]["document_id"]), None
            )
            if key == "quality" and additional:
                rules.append(
                    {
                        "document_id": additional["document_id"],
                        "version": additional["version"],
                        "section_id": additional["section_id"],
                        "quote": additional["content"],
                    }
                )
            items.append(
                {
                    "criterion_id": criterion["id"],
                    "observation": "observed",
                    "score": 0,
                    "reason": "고정된 테스트 결과이며 실제 업무 점수로 사용하지 않습니다.",
                    "work_evidence": [{"source_id": proof["source_id"], "quote": proof["text"]}],
                    "rule_evidence": rules,
                    "review_required": False,
                }
            )
        parsed = AgentEvaluationOutput.model_validate(
            {
                "run_id": payload["run_id"],
                "criteria_sha256": payload["criteria_sha256"],
                "items": items,
                "summary": "테스트 결과",
                "coaching": [
                    {"criterion_id": item["criterion_id"], "action": "검증 과정을 기록하세요."}
                    for item in items
                ],
            }
        )
        chosen = arguments["text_format"].model_validate(selected_result(parsed, payload))
        return SimpleNamespace(status="completed", output=[], output_parsed=chosen)

    return SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(side_effect=parse)))


def approve(pending, action="approve"):
    prompt = pending["__interrupt__"][0].value
    return {
        "action": action,
        **{
            key: prompt[key]
            for key in (
                "criteria_sha256",
                "business_sha256",
                "revision",
            )
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("work_type", ["rfp_analysis", "proposal", "quote", "inquiry_reply"])
async def test_approval_to_evaluator_for_four_types(work_type):
    gateway, client = UnifiedGateway(work_type), client_fixture()
    graph = build_evaluation_workflow(
        gateway, Classifier(work_type), Builder(), evaluator=HandoffEvaluator(client, gateway)
    )
    config = {"configurable": {"thread_id": f"complete-{work_type}"}}
    pending = await graph.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    assert pending["status"] == "awaiting_approval"
    client.responses.parse.assert_not_awaited()
    before = len(gateway.calls)
    result = await graph.ainvoke(Command(resume=approve(pending)), config)
    assert result["status"] == "completed"
    assert len(result["report"]["items"]) == len(result["report"]["coaching"]) == 5
    assert client.responses.parse.await_count == 1
    assert result["evaluation_catalog"]["materials_sha256"]
    assert not any(
        tool in {"get_work_case", "get_artifact_version", "get_conversation"}
        for _, tool, _ in gateway.calls[before:]
    )
    # 평가 모듈이 추가한 공식 자료 인용도 평가 연결부의 최종 검증과 근거 카탈로그에 함께 남는다.
    assert len(result["report"]["items"][4]["rule_evidence"]) == 2


@pytest.mark.asyncio
async def test_revision_and_old_approval_do_not_run_evaluator():
    gateway, client = UnifiedGateway(), client_fixture()
    graph = build_evaluation_workflow(
        gateway, Classifier("proposal"), Builder(), evaluator=HandoffEvaluator(client, gateway)
    )
    config = {"configurable": {"thread_id": "revise-before-evaluate"}}
    first = await graph.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    second = await graph.ainvoke(
        Command(resume={**approve(first, "revise"), "feedback": "명확하게"}), config
    )
    assert second["revision"] == 2
    stale = await graph.ainvoke(Command(resume=approve(first)), config)
    assert stale["__interrupt__"]
    client.responses.parse.assert_not_awaited()
    completed = await graph.ainvoke(Command(resume=approve(second)), config)
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_never_calls_evaluator():
    gateway, client = UnifiedGateway(), client_fixture()
    graph = build_evaluation_workflow(
        gateway, Classifier("proposal"), Builder(), evaluator=HandoffEvaluator(client, gateway)
    )
    config = {"configurable": {"thread_id": "cancel-complete-flow"}}
    pending = await graph.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    result = await graph.ainvoke(Command(resume={"action": "cancel"}), config)
    assert pending["__interrupt__"] and result["status"] == "cancelled"
    client.responses.parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_modified_handoff_is_rejected_before_evaluation():
    gateway, client = UnifiedGateway(), client_fixture()
    graph = build_evaluation_workflow(gateway, Classifier("proposal"), Builder())
    config = {"configurable": {"thread_id": "handoff-tamper"}}
    pending = await graph.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    result = await graph.ainvoke(Command(resume=approve(pending)), config)
    invalid = MCPHandoff.model_validate(result["handoff"])
    invalid.business.artifacts[0]["content"] = "승인 이후 변경"
    with pytest.raises(ValueError):
        await HandoffEvaluator(client, gateway).evaluate(invalid)
    client.responses.parse.assert_not_awaited()


def test_project_dotenv_api_key_wins_over_inherited_default(monkeypatch, tmp_path):
    from ai_agents.shared.settings import setting

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-placeholder")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=project-test-key\n")
    assert setting("OPENAI_API_KEY") == "project-test-key"


@pytest.mark.asyncio
async def test_completed_workflow_does_not_repeat_evaluation():
    gateway, client = UnifiedGateway(), client_fixture()
    graph = build_evaluation_workflow(
        gateway, Classifier("proposal"), Builder(), evaluator=HandoffEvaluator(client, gateway)
    )
    config = {"configurable": {"thread_id": "completed-reuse"}}
    pending = await graph.ainvoke({"work_id": gateway.fixture.context.work_id}, config)
    completed = await graph.ainvoke(Command(resume=approve(pending)), config)
    again = await graph.ainvoke(None, config)
    assert again["report"] == completed["report"]
    assert client.responses.parse.await_count == 1


@pytest.mark.asyncio
async def test_complete_cli_requires_key_before_mcp(monkeypatch, tmp_path):
    from ai_agents.cli.workflow import parser, run

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = parser().parse_args(["--evaluation-id", "test-evaluation"])
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await run(args)
