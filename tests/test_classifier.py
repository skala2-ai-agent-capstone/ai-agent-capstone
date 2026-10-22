import asyncio
import json
from pathlib import Path

import pytest

from ai_agents.classification.agent import (
    ClassificationError,
    ModelReply,
    classify_work,
    validate_evidence,
)
from ai_agents.classification.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from ai_agents.cli.classification import load_context
from ai_agents.shared.models import ClassificationResult, WorkContext

SAMPLE = Path(__file__).resolve().parents[1] / "samples/02_proposal.json"


def valid_result():
    return ClassificationResult(
        status="classified",
        work_type="proposal",
        reason="도입 방안 제안 업무다.",
        evidence=[{"source_id": "m1", "quote": "3단계 도입 일정을 제안서로 작성해줘."}],
        candidate_types=["proposal"],
        missing_information=[],
    )


class FakeBackend:
    model = "test-double-not-a-real-model"

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def classify(self, **kwargs):
        self.calls.append(kwargs)
        return ModelReply(self.result, "test-response")


def test_classifier_calls_backend_and_tracks_metadata():
    context = load_context(SAMPLE)
    backend = FakeBackend(valid_result())
    report = asyncio.run(classify_work(context, backend))
    assert report.classification.work_type == "proposal"
    assert report.work_id == context.work_id
    assert report.prompt_version == PROMPT_VERSION
    assert report.response_id == "test-response"
    assert len(report.input_sha256) == 64
    call = backend.calls[0]
    assert call["system_prompt"] == SYSTEM_PROMPT
    assert "work_id" not in json.loads(call["payload"])
    assert json.loads(call["payload"])["messages"][0]["content"] == context.messages[0].content


@pytest.mark.parametrize("evidence", [
    [{"source_id": "does-not-exist", "quote": "3단계"}],
    [{"source_id": "m1", "quote": "원문에 없는 인용"}],
    [{"source_id": "m1", "quote": "도입"}, {"source_id": "m1", "quote": "도입"}],
])
def test_invalid_evidence_fails_without_success_report(evidence):
    data = valid_result().model_dump()
    data["evidence"] = evidence
    backend = FakeBackend(ClassificationResult.model_validate(data))
    with pytest.raises(ClassificationError):
        asyncio.run(classify_work(load_context(SAMPLE), backend))


def test_metadata_evidence_allowed():
    data = valid_result().model_dump()
    data["evidence"] = [{"source_id": "work:customer_request", "quote": "단계별 일정"}]
    validate_evidence(ClassificationResult.model_validate(data), load_context(SAMPLE))


def test_large_input_not_silently_truncated_or_sent():
    context = WorkContext(work_id="1", customer_request="가" * 80_001)
    backend = FakeBackend(valid_result())
    with pytest.raises(ClassificationError, match="초과"):
        asyncio.run(classify_work(context, backend))
    assert backend.calls == []


def test_input_change_changes_fingerprint():
    first = load_context(SAMPLE)
    second = first.model_copy(update={"title": "변경된 제목"})
    a = asyncio.run(classify_work(first, FakeBackend(valid_result())))
    b = asyncio.run(classify_work(second, FakeBackend(valid_result())))
    assert a.input_sha256 != b.input_sha256

