"""실제 SDK + 가짜 HTTP 전송으로 직렬화·파싱 경계를 검증. 외부 API 호출 없음."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from ai_agents.classification.agent import ClassificationError
from ai_agents.classification.provider import OpenAIClassifier

DECISION = {
    "decision": {
        "status": "classified",
        "task": {
            "work_type": "quotation", "goal": "신규 견적 계산", "evidence_ids": ["e0001"],
        },
    },
    "reason": "신규 금액 산출 요청이다.",
}


def test_real_sdk_serialization_and_structured_parsing_offline():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["store"] is False
        assert body["input"][1]["role"] == "user"
        payload = json.loads(body["input"][1]["content"])
        assert payload["evidence_catalog"][0]["text"] == "금액을 계산해줘."
        return httpx.Response(200, json={
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "test-model",
            "output": [{
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(DECISION, ensure_ascii=False),
                    "annotations": [],
                }],
            }],
        })

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            async with AsyncOpenAI(
                api_key="fake-test-key", http_client=http_client, max_retries=0
            ) as client:
                return await OpenAIClassifier(client, "test-model").classify(
                    system_prompt="분류 규칙",
                    payload=json.dumps({"messages": [
                        {"id": "m1", "role": "user", "content": "금액을 계산해줘."},
                    ]}),
                )

    result = asyncio.run(run())
    assert result.result.work_type == "quotation"
    assert result.response_id == "resp_test"
    assert result.result.candidate_types == ["quotation"]
    assert result.result.evidence[0].quote == "금액을 계산해줘."
    assert result.decision_trace["decision"]["task"]["evidence_ids"] == ["e0001"]
    assert len(requests) == 1


@pytest.mark.parametrize("status,output,parsed", [
    ("incomplete", [], None),
    ("completed", [], None),
    ("completed", [SimpleNamespace(content=[SimpleNamespace(type="refusal")])], None),
])
def test_missing_refused_or_incomplete_output_fails(status, output, parsed):
    response = SimpleNamespace(
        id="test", status=status, output=output, output_parsed=parsed
    )
    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)))
    with pytest.raises(ClassificationError):
        asyncio.run(OpenAIClassifier(client, "test").classify(
            system_prompt="분류 규칙", payload="{}"
        ))


def test_api_error_body_not_disclosed():
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            401, json={"error": {"message": "sensitive-body", "type": "auth_error"}}
        ))
        async with httpx.AsyncClient(transport=transport) as http_client:
            async with AsyncOpenAI(
                api_key="fake-test-key", http_client=http_client, max_retries=0
            ) as client:
                await OpenAIClassifier(client, "test").classify(
                    system_prompt="분류 규칙", payload="{}"
                )

    with pytest.raises(ClassificationError) as error:
        asyncio.run(run())
    assert "401" in str(error.value)
    assert "sensitive-body" not in str(error.value)

