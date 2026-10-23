"""가상 MCP 응답과 고정 모델 응답으로 최종 평가 계약을 재생한다. 모델 품질 측정 아님."""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pydantic import ValidationError

from ..evaluation_agent.final.validation import EvaluationError
from ..integrations.mcp_adapter import MCPToolError
from ..integrations.mcp_client import MCPError
from ..orchestration.final_evaluation import (
    FinalEvaluationJob,
    FinalEvaluationWorkflow,
    PreparedFinalEvaluation,
)


class FixtureMismatch(ValueError):
    """대역이 지원하지 않는 호출은 업무상의 거부 성공으로 계산하지 않는다."""


class ReplayGateway:
    def __init__(self, tape):
        self.tape, self.calls, self.saved = deepcopy(tape), [], []

    async def call(self, server, tool, arguments):
        index = len(self.calls)
        self.calls.append(tool)
        if index >= len(self.tape):
            raise FixtureMismatch("unexpected tool call")
        entry = self.tape[index]
        if (server, tool) != (entry["server"], entry["tool"]):
            raise FixtureMismatch("unexpected tool order")
        if arguments != entry["arguments"]:
            raise FixtureMismatch("unexpected tool arguments")
        if tool.startswith("save_"):
            self.saved.append(deepcopy(arguments))
        return deepcopy(entry["reply"])


class ReplayChat:
    def __init__(self, output):
        self.output, self.calls = deepcopy(output), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(self.output)),
        )])


async def replay_final_evaluation(inputs):
    """로컬 고정 응답만 사용한다. 네트워크·운영 저장소를 호출하지 않는다."""
    if inputs.get("data_policy") != "synthetic":
        raise ValueError("synthetic fixtures required")
    job = FinalEvaluationJob.model_validate(inputs["job"])
    gateway, client = ReplayGateway(inputs["tools"]), ReplayChat(inputs["model_output"])
    with TemporaryDirectory(prefix="final-validation-") as directory:
        workflow = FinalEvaluationWorkflow(
            gateway, client, model="fixture-final-model", checkpoint_dir=Path(directory),
            code_provenance={"validation_mode": "fixture_replay"},
        )
        try:
            result = await workflow.run(job)
        except (EvaluationError, MCPError, MCPToolError, ValidationError) as exc:
            outcome = {"status": "rejected", "error_type": type(exc).__name__}
        else:
            if len(gateway.calls) != len(gateway.tape):
                raise FixtureMismatch("unconsumed tool replies")
            prepared = PreparedFinalEvaluation.model_validate_json(
                Path(result["checkpoint"]).read_text(),
            )
            outcome = {
                "status": result["status"], "review_required": result["review_required"],
                "rubric_version": result["rubric_version"],
                "item_count": len(prepared.report.items),
                "items": [
                    {"criterion_id": i.criterion_id, "observation": i.observation, "score": i.score}
                    for i in prepared.report.items
                ],
            }
    return {
        **outcome, "validation_mode": "fixture_replay", "model_calls": client.calls,
        "save_calls": len(gateway.saved), "tool_calls": gateway.calls,
    }
