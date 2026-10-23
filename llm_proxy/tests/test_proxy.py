import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from proxy.app import create_app, stream_usage
from proxy.settings import Settings, parse_member_keys

KEYS = {"alice-key": "alice", "bob-key": "bob"}


class FakeRecorder:
    def __init__(self, fail=False, works=("TASK-2291", "W-1"), users=("U-kang",)):
        self.records = []
        self.fail = fail
        self.works, self.users = set(works), set(users)

    async def save(self, record):
        if self.fail:
            raise RuntimeError("db down")
        self.records.append(dict(record))

    async def known_refs(self, work_id, user_id):
        return work_id is None or work_id in self.works, user_id is None or user_id in self.users


def settings(**overrides):
    values = dict(
        upstream_url="http://vllm:8000", upstream_api_key="vllm-secret",
        member_keys=KEYS, database_url="unused",
    )
    values.update(overrides)
    return Settings(**values)


def client_for(handler, recorder=None):
    recorder = recorder if recorder is not None else FakeRecorder()
    app = create_app(settings(), recorder=recorder, transport=httpx.MockTransport(handler))
    return TestClient(app), recorder


CHAT_REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gemma4",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "안녕하세요"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
}


def test_rejects_missing_or_wrong_key_without_calling_upstream():
    calls = []
    client, recorder = client_for(lambda r: calls.append(r) or httpx.Response(200, json={}))
    with client:
        assert client.post("/v1/chat/completions", json={}).status_code == 401
        wrong = client.post("/v1/chat/completions", json={},
                            headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_api_key"
    assert calls == [] and recorder.records == []


def test_relays_chat_completion_with_upstream_key_and_records_exchange():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["tags"] = [request.headers.get(h) for h in ("x-stage", "x-work-id", "x-user-id")]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_REPLY)

    client, recorder = client_for(handler)
    payload = {"model": "gemma4", "messages": [{"role": "user", "content": "안녕"}]}
    with client:
        resp = client.post("/v1/chat/completions", json=payload, headers={
            "Authorization": "Bearer alice-key", "X-Stage": "workspace_chat",
            "X-Work-Id": "TASK-2291", "X-User-Id": "U-kang",
        })

    assert resp.status_code == 200 and resp.json() == CHAT_REPLY
    # 팀원 키 대신 vLLM 키로 넘기고, 기록용 헤더는 vLLM에 넘기지 않는다
    assert seen == {"auth": "Bearer vllm-secret", "tags": [None, None, None], "body": payload}
    [record] = recorder.records
    assert record["member"] == "alice"
    assert record["endpoint"] == "/v1/chat/completions"
    assert (record["stage"], record["work_id"], record["user_id"]) == (
        "workspace_chat", "TASK-2291", "U-kang")
    assert record["model"] == "gemma4"
    assert (record["input_tokens"], record["output_tokens"]) == (12, 3)
    assert record["request"] == payload and record["response"] == CHAT_REPLY
    assert record["status_code"] == 200 and record["error"] is None
    assert "alice-key" not in json.dumps(record, ensure_ascii=False)


def test_records_responses_api_token_names():
    reply = {"id": "resp_1", "status": "completed", "output": [],
             "usage": {"input_tokens": 40, "output_tokens": 9}}
    client, recorder = client_for(lambda r: httpx.Response(200, json=reply))
    with client:
        client.post("/v1/responses", json={"model": "gemma4", "input": "hi"},
                    headers={"Authorization": "Bearer bob-key"})
    [record] = recorder.records
    assert record["member"] == "bob" and record["endpoint"] == "/v1/responses"
    assert (record["input_tokens"], record["output_tokens"]) == (40, 9)


def test_upstream_error_status_is_passed_through_and_marked():
    body = {"error": {"message": "bad schema"}}
    client, recorder = client_for(lambda r: httpx.Response(400, json=body))
    with client:
        resp = client.post("/v1/chat/completions", json={"model": "gemma4"},
                           headers={"Authorization": "Bearer alice-key"})
    assert resp.status_code == 400 and resp.json() == body
    assert recorder.records[0]["error"] == "upstream_error"


@pytest.mark.parametrize("exc, status, code", [
    (httpx.ConnectError, 502, "upstream_unreachable"),
    (httpx.ReadTimeout, 504, "upstream_timeout"),
])
def test_upstream_failures_return_openai_style_error_and_are_recorded(exc, status, code):
    def handler(request):
        raise exc("boom", request=request)

    client, recorder = client_for(handler)
    with client:
        resp = client.post("/v1/chat/completions", json={"model": "gemma4"},
                           headers={"Authorization": "Bearer alice-key"})
    assert resp.status_code == status and resp.json()["error"]["code"] == code
    assert recorder.records[0]["status_code"] == status
    assert recorder.records[0]["error"] == code


def test_streaming_is_relayed_and_raw_events_are_recorded():
    events = (
        'data: {"choices":[{"delta":{"content":"안"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"녕"}}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    client, recorder = client_for(lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=events))
    with client:
        resp = client.post("/v1/chat/completions", json={"model": "gemma4", "stream": True},
                           headers={"Authorization": "Bearer alice-key"})
    assert resp.content == events
    [record] = recorder.records
    assert record["stream"] is True and record["response"] is None
    assert record["response_text"] == events.decode()
    assert (record["input_tokens"], record["output_tokens"]) == (5, 2)


def test_stream_usage_reads_responses_api_completed_event():
    text = 'event: response.completed\ndata: {"type":"response.completed",' \
           '"response":{"usage":{"input_tokens":7,"output_tokens":4}}}\n\n'
    assert stream_usage(text) == (7, 4)


def test_model_list_is_forwarded_but_not_recorded():
    models = {"object": "list", "data": [{"id": "gemma4"}]}
    client, recorder = client_for(lambda r: httpx.Response(200, json=models))
    with client:
        resp = client.get("/v1/models", headers={"Authorization": "Bearer bob-key"})
    assert resp.json() == models and recorder.records == []


def test_recorder_failure_does_not_break_the_reply():
    client, _ = client_for(lambda r: httpx.Response(200, json=CHAT_REPLY), FakeRecorder(fail=True))
    with client:
        resp = client.post("/v1/chat/completions", json={"model": "gemma4"},
                           headers={"Authorization": "Bearer alice-key"})
    assert resp.status_code == 200


def test_parse_member_keys():
    assert parse_member_keys(" alice:k1, bob:k2 ,") == {"k1": "alice", "k2": "bob"}
    for bad in ["", "alice", "alice:", ":k1", "alice:k1,bob:k1"]:
        with pytest.raises(ValueError):
            parse_member_keys(bad)


@pytest.mark.parametrize("headers, message", [
    ({"X-Stage": "chat"}, "X-Stage"),
    ({"X-Stage": "workspace_chat", "X-User-Id": "U-kang"}, "X-Work-Id와 X-User-Id"),
    ({"X-Stage": "workspace_chat", "X-Work-Id": "TASK-2291"}, "X-Work-Id와 X-User-Id"),
    ({"X-Stage": "classify", "X-Work-Id": "TASK-9999"}, "없는 업무"),
    ({"X-Stage": "workspace_chat", "X-Work-Id": "TASK-2291", "X-User-Id": "U-nobody"},
     "없는 사용자"),
])
def test_rejects_trace_headers_the_db_cannot_store_before_calling_upstream(headers, message):
    calls = []
    client, recorder = client_for(lambda r: calls.append(r) or httpx.Response(200, json=CHAT_REPLY))
    with client:
        resp = client.post("/v1/chat/completions", json={"model": "gemma4"},
                           headers={"Authorization": "Bearer alice-key", **headers})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_trace_headers"
    assert message in resp.json()["error"]["message"]
    assert calls == [] and recorder.records == []


def test_agent_call_without_user_is_allowed():
    client, recorder = client_for(lambda r: httpx.Response(200, json=CHAT_REPLY))
    with client:
        resp = client.post("/v1/responses", json={"model": "gemma4"},
                           headers={"Authorization": "Bearer alice-key", "X-Stage": "evaluate",
                                    "X-Work-Id": "W-1"})
    assert resp.status_code == 200
    assert (recorder.records[0]["work_id"], recorder.records[0]["user_id"]) == ("W-1", None)


@pytest.mark.skipif(not os.getenv("LLM_PROXY_TEST_DATABASE_URL"),
                    reason="실제 Postgres 검증은 시드를 적재한 업무 DB 주소"
                           "(LLM_PROXY_TEST_DATABASE_URL)가 있을 때만")
def test_postgres_recorder_against_business_db():
    import asyncio

    import psycopg
    from proxy.store import PostgresRecorder

    url = os.environ["LLM_PROXY_TEST_DATABASE_URL"]
    record = {
        "member": "pytest", "endpoint": "/v1/responses", "stage": "classify",
        "work_id": "W-E2E-RFP-01", "user_id": "E-1021", "model": "gemma4", "stream": False,
        "status_code": 200, "error": None, "latency_ms": 12, "input_tokens": 1, "output_tokens": 2,
        "request": {"input": [{"content": "한글"}]}, "response": CHAT_REPLY, "response_text": None,
    }

    async def run():
        recorder = PostgresRecorder(url)
        await recorder.open()
        try:
            refs = [await recorder.known_refs("W-E2E-RFP-01", "E-1021"),
                    await recorder.known_refs("W-NOPE", "NOPE"),
                    await recorder.known_refs(None, None)]
            await recorder.save(record)
            return refs
        finally:
            await recorder.close()

    refs = asyncio.run(run())
    assert refs == [(True, True), (False, False), (True, True)]
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "DELETE FROM llm_exchanges WHERE member = 'pytest'"
            " RETURNING user_id, request, response").fetchone()
    assert row == ("E-1021", record["request"], CHAT_REPLY)
