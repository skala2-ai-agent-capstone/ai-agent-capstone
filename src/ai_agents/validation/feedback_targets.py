"""가상 계약 재생과 실제 모델 품질 평가 target. 두 실행의 의미를 구분한다."""

import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from openai import AsyncOpenAI

from ..evaluation_agent.feedback.agent import FeedbackAgent
from ..evaluation_agent.feedback.errors import FeedbackError
from ..orchestration.live_feedback import LiveFeedbackWorkflow, content_digest
from ..shared.live_feedback_contracts import (
    FeedbackRubric,
    LiveEvaluationContext,
    LiveEvaluationEvent,
    MessageBatch,
)


class FixtureGateway:
    """DB의 동작을 검증하는 구현이 아니다. Agent 계약 테스트용 고정 응답이다."""

    def __init__(self, fixture):
        self.fixture = deepcopy(fixture)
        self.calls = []
        self.saved = None

    async def call(self, tool, arguments):
        self.calls.append(tool)
        if tool == "start_live_evaluation":
            return deepcopy(self.fixture["context"])
        if tool == "get_messages":
            return deepcopy(self.fixture["batch"])
        if tool == "save_live_evaluation":
            self.saved = deepcopy(arguments)
            return {"live_eval_id": arguments["live_eval_id"], "status": "완료", "duplicate": False}
        raise AssertionError("unexpected tool")


class FixtureChatClient:
    def __init__(self, result):
        self.result, self.calls = result, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(self.result)),
                )
            ],
            usage=None,
        )


async def replay_feedback(inputs):
    gateway = FixtureGateway(inputs)
    client = FixtureChatClient(inputs["model_output"])
    agent = FeedbackAgent(client, "fixture-model")
    try:
        result = await LiveFeedbackWorkflow(gateway, agent).run(
            LiveEvaluationEvent.model_validate(inputs["event"]),
            trace_id="fixture-trace",
            code_provenance={"git_commit": "fixture"},
        )
    except FeedbackError as exc:
        result = {"status": "rejected", "code": exc.code}
    return {
        **result,
        "model_calls": len(client.calls),
        "tool_calls": gateway.calls,
        "saved": gateway.saved is not None,
    }


async def model_feedback(inputs):
    """명시적으로 실행하면 실제 모델을 호출한다. 판정은 별도 사람이 기록해야 한다."""
    model, base_url = os.getenv("FEEDBACK_LLM_MODEL"), os.getenv("FEEDBACK_LLM_BASE_URL")
    if not model or not base_url:
        raise ValueError("feedback model configuration required")
    context_data = deepcopy(inputs["context"])
    # 고정 대화 평가셋을 유지하면서 팀원이 배포한 기준 버전만 바꿔 비교할 수 있다.
    rubric_path = os.getenv("FEEDBACK_RUBRIC_FILE")
    if rubric_path:
        rubric = FeedbackRubric.model_validate_json(Path(rubric_path).read_text())
        context_data.update(
            rubric_doc_id=rubric.doc_id,
            rubric_version=rubric.version,
            rubric=rubric.model_dump(mode="json"),
        )
    context = LiveEvaluationContext.model_validate(context_data)
    async with AsyncOpenAI(
        base_url=base_url,
        api_key=os.getenv("FEEDBACK_LLM_API_KEY") or "self-hosted",
        timeout=45,
        max_retries=0,
    ) as client:
        # 가상 업무 ID는 운영 DB의 FK를 만족하지 않는다. 단계만 기록한다.
        result = await FeedbackAgent(client, model, record_work_refs=False).generate(
            context,
            MessageBatch.model_validate(inputs["batch"]),
        )
    return {
        "feedback": result.model_dump(mode="json"),
        "model": model,
        "rubric_version": context.rubric_version,
        "rubric_sha256": content_digest(
            context.rubric.model_dump(mode="json") if context.rubric else None
        ),
    }
