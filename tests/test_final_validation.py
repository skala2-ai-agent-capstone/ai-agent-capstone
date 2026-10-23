import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai_agents.cli.validation import main
from ai_agents.validation.final_targets import FixtureMismatch, replay_final_evaluation
from ai_agents.validation.models import CaseResult, Dataset, ExperimentResult
from ai_agents.validation.report import render_report
from ai_agents.validation.runner import check_output

ROOT = Path(__file__).resolve().parents[1]
DATA = Dataset.model_validate_json((ROOT / "samples/final/functional-v1.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("example", DATA.examples, ids=lambda example: example.case_id)
async def test_final_contract_dataset_matches_expectations(example):
    original = deepcopy(example.inputs)
    output = await replay_final_evaluation(example.inputs)
    checks = check_output({"status": "ok", "output": output}, example.checks)
    assert checks and all(check["passed"] for check in checks)
    assert example.inputs == original


@pytest.mark.asyncio
async def test_replay_fixture_drift_is_not_counted_as_expected_rejection():
    inputs = deepcopy(DATA.examples[0].inputs)
    inputs["tools"][0]["arguments"]["evaluation_id"] = "wrong"
    with pytest.raises(FixtureMismatch):
        await replay_final_evaluation(inputs)


def experiment():
    return ExperimentResult(
        manifest={
            "experiment_id": "exp-1", "purpose": "final", "dataset_id": "final-fixed",
            "dataset_version": "1", "dataset_sha256": "same-data", "data_version": "1",
            "model": "fixture", "model_settings": {}, "prompt_version": "1",
            "rubric_version": "1", "git_commit": "abc123", "dirty": False,
            "working_tree_sha256": "tree-1",
            "target": "ai_agents.validation.final_targets:replay_final_evaluation",
        },
        cases=[CaseResult(
            case_id="case-1", category="normal", verdict="passed",
            expected_description="PRIVATE input description", evidence_ids=[],
            actual={"raw": "PRIVATE raw conversation"}, checks=[],
            duration_ms=10, trace_id="trace-1",
        )],
    )


def test_report_has_provenance_without_copying_inputs_or_claiming_quality():
    result = experiment()
    result.manifest["dirty"] = True
    rendered = render_report(result)
    assert "abc123" in rendered and "tree-1" in rendered and "trace-1" in rendered
    assert "미커밋 변경이 포함됐다" in rendered
    assert "가상 MCP·고정 모델 응답 재생" in rendered
    assert "PRIVATE" not in rendered


def test_report_tracks_regressions_and_unreviewed_separately():
    before = experiment()
    after = before.model_copy(deep=True)
    after.cases[0].verdict = "failed"
    assert "통과 → 실패: case-1" in render_report(after, before=before)
    after.cases[0].verdict = "unreviewed"
    report = render_report(after, before=before)
    assert "미검토 상태 변경: case-1" in report
    assert "통과 → 실패: 없음" in report
    assert "0.0%" in report


@pytest.mark.parametrize("field", ["dataset_sha256", "rubric_version", "model"])
def test_report_rejects_incomparable_experiments(field):
    before, after = experiment(), experiment()
    after.manifest[field] = "changed"
    with pytest.raises(ValueError, match="incompatible"):
        render_report(after, before=before)
    if field != "dataset_sha256":
        assert "허용한 설정 변경" in render_report(after, before=before, allow_changes=[field])


@pytest.mark.parametrize("duration", [-1, float("nan"), float("inf")])
def test_invalid_duration_cannot_be_reported(duration):
    result = experiment()
    result.cases[0].duration_ms = duration
    with pytest.raises(ValueError, match="duration"):
        render_report(result)


def test_report_cli_preserves_existing_file(tmp_path, capsys):
    source, target = tmp_path / "experiment.json", tmp_path / "report.md"
    source.write_text(experiment().model_dump_json())
    args = ["report", str(source), "--output", str(target)]
    assert main(args) == 0
    original = target.read_text()
    assert main(args) == 2
    assert target.read_text() == original
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error_type"] == "FileExistsError"
