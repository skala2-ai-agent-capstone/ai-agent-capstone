import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from work_classifier.cli import load_context
from work_classifier.models import ClassificationResult, WorkContext

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted((ROOT / "samples").glob("*.json"))


def decision(**updates):
    data = {
        "status": "classified",
        "work_type": "proposal",
        "reason": "고객 맞춤 도입 방안을 작성한다.",
        "evidence": [{"source_id": "m1", "quote": "도입 방안"}],
        "candidate_types": ["proposal"],
        "missing_information": [],
    }
    return {**data, **updates}


@pytest.mark.parametrize("path", SAMPLES, ids=lambda path: path.name)
def test_sample_inputs_have_no_labels(path):
    context = load_context(path)
    payload = context.llm_payload()
    assert "work_id" not in payload
    assert "work_type" not in payload
    assert "expected" not in payload
    assert context.evidence_sources()


def test_expected_labels_cover_samples_separately():
    labels = json.loads((ROOT / "tests/fixtures/expected_labels.json").read_text())
    assert set(labels) == {p.name for p in SAMPLES}
    assert {v["work_type"] for v in labels.values()} >= {
        "rfp_analysis", "proposal", "quotation", "customer_response"
    }


@pytest.mark.parametrize("data", [
    {"work_id": "1"},
    {"work_id": "1", "title": "   "},
    {"work_id": "1", "title": "작업", "work_type": "proposal"},
    {"work_id": "1", "title": "작업", "coverage": "complete", "missing_sources": ["대화"]},
    {"work_id": "1", "messages": [
        {"id": "m1", "role": "user", "content": "도입"},
        {"id": "m1", "role": "assistant", "content": "답변"},
    ]},
    {"work_id": "1", "messages": [
        {"id": "work:title", "role": "user", "content": "도입"},
    ]},
    {"work_id": "1", "messages": [
        {"id": "a1", "role": "user", "content": "도입"},
    ], "artifacts": [{"id": "a1", "content": "도입안"}]},
    {"work_id": "1", "artifacts": [{"id": "a1", "version": 0, "content": "도입안"}]},
])
def test_bad_input_rejected(data):
    with pytest.raises(ValidationError):
        WorkContext.model_validate(data)


@pytest.mark.parametrize("updates", [
    {"work_type": None},
    {"work_type": "email"},
    {"candidate_types": ["quotation"]},
    {"candidate_types": ["proposal", "proposal"]},
    {"evidence": []},
    {"status": "needs_review"},
    {"status": "needs_review", "work_type": None, "missing_information": []},
    {"status": "multiple_tasks", "work_type": None},
    {"status": "out_of_scope", "work_type": None},
    {"reason": "  "},
    {"confidence": 0.9},
])
def test_bad_result_rejected(updates):
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate(decision(**updates))


@pytest.mark.parametrize("updates", [
    {},
    {"status": "needs_review", "work_type": None, "candidate_types": [],
     "evidence": [], "missing_information": ["원래 고객 요청"]},
    {"status": "multiple_tasks", "work_type": None,
     "candidate_types": ["proposal", "rfp_analysis"]},
    {"status": "out_of_scope", "work_type": None, "candidate_types": []},
])
def test_all_four_statuses_valid(updates):
    ClassificationResult.model_validate(decision(**updates))

