"""모의 데이터가 docs/llm_exchanges_data_spec.md의 명세를 지키는지 확인한다."""

import json
from datetime import datetime
from pathlib import Path

import pytest

MOCK = Path(__file__).resolve().parents[2] / "mcp_servers/data/llm_exchanges.jsonl"

# 컬럼: (JSON 타입, NULL 허용)
COLUMNS = {
    "id": (int, False), "created_at": (str, False), "member": (str, False),
    "endpoint": (str, False), "stage": (str, True), "work_id": (str, True),
    "user_id": (str, True), "model": (str, True), "stream": (bool, False),
    "status_code": (int, True), "error": (str, True), "latency_ms": (int, False),
    "input_tokens": (int, True), "output_tokens": (int, True), "request": (dict, True),
    "response": (dict, True), "response_text": (str, True),
}
STAGES = {"workspace_chat", "classify", "rubric", "evaluate", None}
ERRORS = {None, "upstream_error", "upstream_unreachable", "upstream_timeout",
          "upstream_stream_broken", "client_disconnected"}


@pytest.fixture(scope="module")
def rows():
    if not MOCK.exists():
        pytest.skip("모의 데이터가 없습니다. build_mock.py를 먼저 실행하세요.")
    return [json.loads(line) for line in MOCK.read_text().splitlines()]


def test_columns_and_types(rows):
    for row in rows:
        assert list(row) == list(COLUMNS)
        for name, (kind, nullable) in COLUMNS.items():
            value = row[name]
            if value is None:
                assert nullable, (row["id"], name)
            else:
                # bool은 int의 하위 타입이라 정확히 비교한다
                assert type(value) is kind, (row["id"], name, type(value))
        assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
        for name in ("stage", "work_id", "user_id"):
            assert row[name] is None or 0 < len(row[name]) <= 128


def test_ids_and_order(rows):
    assert [r["id"] for r in rows] == list(range(1, len(rows) + 1))
    times = [datetime.fromisoformat(r["created_at"]) for r in rows]
    assert times == sorted(times)


def test_code_values(rows):
    for row in rows:
        assert row["stage"] in STAGES and row["error"] in ERRORS
        if row["stage"] == "workspace_chat":
            assert row["endpoint"] == "/v1/chat/completions" and row["user_id"]
        elif row["stage"] in {"classify", "rubric", "evaluate"}:
            assert row["endpoint"] == "/v1/responses" and row["user_id"] is None
        if row["stage"] in {"classify", "rubric"}:
            # 분류 힌트가 되지 않도록 업무 ID는 헤더(work_id 칸)로만 기록한다
            assert row["work_id"] not in json.dumps(row["request"], ensure_ascii=False)


def test_value_relations(rows):
    for row in rows:
        if row["stream"]:
            assert row["response"] is None and row["response_text"].startswith("data: ")
        if row["error"] in {"upstream_unreachable", "upstream_timeout"}:
            assert row["status_code"] in {502, 504}
            assert row["input_tokens"] is row["output_tokens"] is row["response"] is None
        if row["error"] == "upstream_error":
            assert row["status_code"] >= 400 and row["response"]["object"] == "error"
        if row["error"] is None:
            assert 200 <= row["status_code"] < 300
            assert row["input_tokens"] > 0 and row["output_tokens"] > 0


def test_usage_matches_body(rows):
    for row in rows:
        body = row["response"]
        if row["error"] or not body:
            continue
        usage = body["usage"]
        expected = (
            (usage["prompt_tokens"], usage["completion_tokens"])
            if row["endpoint"] == "/v1/chat/completions"
            else (usage["input_tokens"], usage["output_tokens"])
        )
        assert (row["input_tokens"], row["output_tokens"]) == expected
