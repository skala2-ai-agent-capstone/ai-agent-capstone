from copy import deepcopy

import pytest

from ai_agents.cli.e2e import adapt_case


@pytest.fixture
def raw_case():
    return {
        "case_id": "E2E-QT-01", "work_type": "quote", "summary": "정답 힌트",
        "raw": {
            "work_case": {
                "work_id": "W-E2E-QT-01", "work_type": "quote", "status": "completed",
                "registration": {"quantity": "50명", "work_type": "quote"},
                "source_message": {"content": "연락처 010-1234-5678로 알려주세요."},
            },
            "messages": [{
                "message_id": "W-E2E-QT-01-M01", "seq": 1,
                "role": "user", "content": "50명 견적을 작성해줘. W-E2E-QT-01",
            }],
            "artifact_versions": [
                {"artifact_id": "ART-E2E-QT-01", "version": "v10",
                 "author": "employee", "content": "최종 금액"},
                {"artifact_id": "ART-E2E-QT-01", "version": "v2",
                 "author": "ai_draft", "content": "초안 금액"},
            ],
            "source_documents": [{"doc_id": "RFP-01", "version": "v1.2",
                                  "content": "요구사항 원문"}],
            "human_actions": [{"decision": "점수 수정"}],
            "fault_injection": [{"instruction": "정답 힌트"}],
        },
    }


def test_labels_and_review_actions_do_not_change_model_input(raw_case):
    before, _ = adapt_case(raw_case, [])
    changed = deepcopy(raw_case)
    changed["work_type"] = "proposal"
    changed["summary"] = "다른 정답"
    changed["raw"]["work_case"]["work_type"] = "proposal"
    changed["raw"]["work_case"]["registration"]["work_type"] = "proposal"
    changed["raw"]["human_actions"] = []
    after, _ = adapt_case(changed, [])
    assert before.llm_payload() == after.llm_payload()
    payload = str(before.llm_payload())
    assert "E2E-QT" not in payload
    assert "정답 힌트" not in payload
    assert "점수 수정" not in payload
    assert "work_type" not in payload


def test_versions_references_and_original_source_mapping(raw_case):
    context, audit = adapt_case(raw_case, [])
    assert context.artifacts[0].version == 10
    assert context.artifacts[0].stage == "final"
    assert context.artifacts[1].stage == "draft"
    assert context.artifacts[2].stage == "reference"
    assert context.artifacts[3].stage == "reference"
    assert audit["source_mapping"]["a001"]["version"] == "v10"
    assert audit["source_mapping"]["r001"]["version"] == "v1.2"
    assert audit["source_mapping"]["m001"]["message_id"] == "W-E2E-QT-01-M01"


def test_masking_applies_before_model_receives_text(raw_case):
    context, audit = adapt_case(raw_case, [{
        "kind": "masking", "rule_id": "phone", "pattern": r"010-\d{4}-\d{4}",
        "replace_with": "[연락처]",
    }])
    assert "010-1234-5678" not in str(context.llm_payload())
    assert "[연락처]" in context.artifacts[-1].content
    assert audit["masking_counts"]["phone"] == 1


def test_invalid_version_is_not_silently_coerced(raw_case):
    raw_case["raw"]["artifact_versions"][0]["version"] = "v1.2"
    with pytest.raises(ValueError, match="버전"):
        adapt_case(raw_case, [])
