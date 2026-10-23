"""실제 SDK → 프록시 HTTP 계약 검증. DB와 모델 서버는 가짜 구현으로 대체한다.

llm_proxy/requirements.txt의 선택 의존성이 설치된 환경에서 실행한다.
"""

import json
from importlib import import_module
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from ai_agents.evaluation_agent.feedback.agent import FeedbackAgent
from ai_agents.shared.live_feedback_contracts import LiveEvaluationContext, MessageBatch
from ai_agents.validation import feedback_targets

pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")
pytest.importorskip("psycopg")
create_app = import_module("llm_proxy.proxy.app").create_app
Settings = import_module("llm_proxy.proxy.settings").Settings
ROOT = Path(__file__).resolve().parents[1]


class MemoryRecorder:
    def __init__(self):
        self.records = []

    async def known_refs(self, work_id, user_id):
        return work_id in (None, "W-DEMO"), user_id in (None, "U-DEMO")

    async def save(self, record):
        self.records.append(dict(record))


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic", [False, True])
async def test_feedback_sdk_records_proxy_metadata_without_forwarding_it(monkeypatch, synthetic):
    fixture = json.loads((ROOT / "samples/feedback/complete-five.json").read_text())
    recorder, upstream_requests = MemoryRecorder(), []

    def upstream(request):
        upstream_requests.append(request)
        return httpx.Response(200, json={
            "id": "mock-completion", "object": "chat.completion", "created": 1,
            "model": "mock-model", "choices": [{
                "index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(fixture["model_output"]),
                },
            }],
        })

    app = create_app(
        Settings(upstream_url="http://model", upstream_api_key="model-key",
                 member_keys={"member-key": "tester"}, database_url="unused"),
        recorder=recorder, transport=httpx.MockTransport(upstream),
    )

    def sdk_client(**kwargs):
        return AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)),
        )

    async with app.router.lifespan_context(app):
        if synthetic:
            # 실제 품질 평가 진입점이 가상 업무 ID를 전송하지 않는지도 검증한다.
            fixture["context"]["work_id"] = fixture["batch"]["work_id"] = "W-SYNTHETIC"
            monkeypatch.setenv("FEEDBACK_LLM_BASE_URL", "http://proxy/v1")
            monkeypatch.setenv("FEEDBACK_LLM_API_KEY", "member-key")
            monkeypatch.setenv("FEEDBACK_LLM_MODEL", "mock-model")
            monkeypatch.delenv("FEEDBACK_RUBRIC_FILE", raising=False)
            monkeypatch.setattr(feedback_targets, "AsyncOpenAI", sdk_client)
            await feedback_targets.model_feedback(fixture)
        else:
            async with sdk_client(base_url="http://proxy/v1", api_key="member-key") as client:
                await FeedbackAgent(client, "mock-model").generate(
                    LiveEvaluationContext.model_validate(fixture["context"]),
                    MessageBatch.model_validate(fixture["batch"]),
                )

    [record] = recorder.records
    assert record["stage"] == "live_evaluate"
    assert record["work_id"] == (None if synthetic else "W-DEMO")
    assert record["user_id"] == (None if synthetic else "U-DEMO")
    assert record["member"] == "tester" and record["status_code"] == 200
    [request] = upstream_requests
    assert request.headers["authorization"] == "Bearer model-key"
    assert all(name not in request.headers for name in ("x-stage", "x-work-id", "x-user-id"))
