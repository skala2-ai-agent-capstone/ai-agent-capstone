import json

import pytest
from pydantic import ValidationError

from work_classifier.policy import load_policy_chunks, normalize_section, split_policy_lines


def test_split_preserves_paragraph_pages_and_skips_toc_noise():
    policy = split_policy_lines([
        (1, "제6조 정보 보호"), (1, "별표 5 점검 기준"), (2, "제2장 보호"),
        (2, "제6조 (정보 보호)"), (2, "서문"), (2, "① 원문 첫째"),
        (2, "2 / 3"), (3, "가상 문서 · 교육용 시연 자료"),
        (3, "이어지는 문장"), (3, "② 원문 둘째"),
        (3, "별표 5. 기준"), (3, "0/1/2점"),
    ], "policy", "v1")
    assert [c.chunk_id for c in policy.chunks] == ["ART-06", "ART-06-1", "ART-06-2", "APX-5"]
    assert policy.chunks[1].text == "① 원문 첫째\n이어지는 문장"
    assert policy.chunks[2].page == 3
    assert len(policy.resolve("ART-6")) == 3  # 서문이 있어도 모든 항을 보존
    assert [c.chunk_id for c in policy.resolve("ART-6-2")] == ["ART-06-2"]
    assert normalize_section(" art-6-2 ") == "ART-06-2"


def test_heading_same_line_text_is_not_dropped():
    policy = split_policy_lines([(1, "제6조 (정보 보호) ① 기밀 금지")], "p", "v1")
    assert policy.chunks[0].chunk_id == "ART-06-1"
    assert policy.chunks[0].text == "① 기밀 금지"


def test_empty_and_duplicate_sections_fail():
    with pytest.raises(ValidationError):
        split_policy_lines([], "p", "v1")
    with pytest.raises(ValidationError, match="중복"):
        split_policy_lines([
            (1, "제6조 (보호)"), (1, "① 첫째"), (1, "① 중복"),
        ], "p", "v1")


def test_load_notebook_chunks_and_reject_mixed_versions(tmp_path):
    bundle = split_policy_lines([(1, "제6조 (보호)"), (1, "① 첫째"), (1, "② 둘째")], "p", "v1")
    raw = [c.model_dump() for c in bundle.chunks]
    path = tmp_path / "policy_chunks.json"
    path.write_text(json.dumps(raw))
    assert load_policy_chunks(path) == bundle
    raw[1]["version"] = "v2"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValidationError, match="한 버전"):
        load_policy_chunks(path)
