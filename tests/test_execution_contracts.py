import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_agents.shared.execution_contracts import (
    EvaluationRequest,
    FeedbackResult,
    canonical_work_type,
)

SAMPLE = Path(__file__).resolve().parents[1] / "samples/validation/contracts-v1.json"


@pytest.fixture
def request_data():
    return json.loads(SAMPLE.read_text())["examples"][0]["inputs"]


def test_feedback_without_artifact_and_final_rejection(request_data):
    request = EvaluationRequest.model_validate(request_data)
    assert request.snapshot.artifact_ref is None
    request_data["purpose"] = "final"
    with pytest.raises(ValidationError, match="requires an artifact"):
        EvaluationRequest.model_validate(request_data)


@pytest.mark.parametrize("bounds", [(0, 2), (2, 0), (5, 2), (-1, 0), (True, 2)])
def test_invalid_conversation_range(request_data, bounds):
    request_data["snapshot"]["conversation_range"] = dict(zip(("from_seq", "to_seq"), bounds))
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(request_data)


def test_contract_is_frozen_and_forbids_credentials(request_data):
    request = EvaluationRequest.model_validate(request_data)
    with pytest.raises(ValidationError):
        request.snapshot.conversation_range.to_seq = 99
    request_data["token"] = "not-allowed"
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(request_data)


def test_versions_approval_and_timezone(request_data):
    for modify in (
        lambda x: x["snapshot"]["rubric"].update(work_type="quote"),
        lambda x: x["snapshot"]["rubric"].update(approved_at="2027-01-01T00:00:00Z"),
        lambda x: x["snapshot"].update(captured_at="2026-10-22T10:00:00"),
        lambda x: x["snapshot"].update(reference_refs=[{"doc_id": "D", "version": "v1"}] * 2),
    ):
        bad = deepcopy(request_data)
        modify(bad)
        with pytest.raises(ValidationError):
            EvaluationRequest.model_validate(bad)


def feedback(request):
    return FeedbackResult(
        request_id=request.request_id,
        snapshot_id=request.snapshot.snapshot_id,
        snapshot_sha256=request.snapshot.content_sha256,
        run_id="run-1",
        status="completed",
        items=[
            dict(
                item_id="instruction",
                kind="improvement",
                target_ids=["M01"],
                evidence_ids=["D1@v2#p1"],
                diagnosis="출력 형식이 없음",
                next_action="형식 명시",
            )
        ],
    )


def test_feedback_scope_rejects_cross_snapshot_and_invented_evidence(request_data):
    request = EvaluationRequest.model_validate(request_data)
    result = feedback(request)
    result.validate_scope(request, evidence_ids={"M01", "D1@v2#p1"}, item_ids={"instruction"})
    with pytest.raises(ValueError, match="outside snapshot"):
        result.validate_scope(request, evidence_ids={"M01"}, item_ids={"instruction"})
    changed = deepcopy(request_data)
    changed["snapshot"]["content_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="snapshot"):
        result.validate_scope(
            EvaluationRequest.model_validate(changed),
            evidence_ids={"M01", "D1@v2#p1"},
            item_ids={"instruction"},
        )


def test_feedback_cannot_claim_completion_without_results(request_data):
    result = feedback(EvaluationRequest.model_validate(request_data)).model_dump(mode="json")
    result["items"] = []
    with pytest.raises(ValidationError):
        FeedbackResult.model_validate(result)
    result["status"] = "insufficient_data"
    with pytest.raises(ValidationError):
        FeedbackResult.model_validate(result)
    result["missing_sources"] = ["conversation"]
    FeedbackResult.model_validate(result)


def test_work_type_aliases_are_explicit():
    assert canonical_work_type("quotation") == "quote"
    assert canonical_work_type("customer_response") == "inquiry_reply"
    with pytest.raises(ValueError):
        canonical_work_type("unknown")
