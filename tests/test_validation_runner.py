import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from work_classifier.validation.cli import main
from work_classifier.validation.models import Check, Dataset
from work_classifier.validation.runner import (
    check_output,
    compare_experiments,
    git_provenance,
    run_experiment,
)
from work_classifier.validation.targets import validate_request

SAMPLE = Path(__file__).resolve().parents[1] / "samples/validation/contracts-v1.json"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "test.txt").write_text("baseline")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=root,
        check=True,
    )
    return root


async def run(root, dest, dataset=None, target=validate_request, **kwargs):
    return await run_experiment(
        dataset or Dataset.model_validate_json(SAMPLE.read_text()),
        target,
        output_dir=dest,
        repo=root,
        label="unit-test",
        target_name="test:target",
        model="none",
        prompt_version="none",
        rubric_version="fixture-v2",
        data_version="fixture-v1",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_real_contract_runner_exports_and_compares(repo, tmp_path):
    before = await run(repo, tmp_path / "before")
    after = await run(repo, tmp_path / "after")
    assert all(c.verdict == "passed" for c in before.cases)
    assert len(before.cases) == 5
    assert (tmp_path / "before/report.csv").exists()
    assert len((tmp_path / "before/results.jsonl").read_text().splitlines()) == 5
    comparison = compare_experiments(before, after)
    assert comparison["before_pass_rate"] == 1
    assert comparison["regressed"] == []
    assert comparison["dirty_warning"] is False
    with pytest.raises(FileExistsError):
        await run(repo, tmp_path / "before")


@pytest.mark.asyncio
async def test_expected_output_is_not_leaked_to_target_and_unreviewed_not_passed(repo, tmp_path):
    raw = json.loads(SAMPLE.read_text())
    raw["examples"] = raw["examples"][:1]
    raw["examples"][0]["checks"] = []
    called = []

    def target(inputs):
        called.append(deepcopy(inputs))
        inputs["mutated"] = True
        return {"ok": True}

    dataset = Dataset.model_validate(raw)
    result = await run(repo, tmp_path / "run", dataset, target)
    assert result.cases[0].verdict == "unreviewed"
    assert "expected_description" not in called[0]
    assert "mutated" not in dataset.examples[0].inputs
    assert compare_experiments(result, result)["before_pass_rate"] == 0


@pytest.mark.asyncio
async def test_comparison_rejects_changed_dataset_and_requires_declared_changes(repo, tmp_path):
    original = await run(repo, tmp_path / "run")
    changed = original.model_copy(deep=True)
    changed.manifest["dataset_sha256"] = "different"
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare_experiments(original, changed)
    changed = original.model_copy(deep=True)
    changed.manifest["rubric_version"] = "v3"
    with pytest.raises(ValueError, match="rubric_version"):
        compare_experiments(original, changed)
    comparison = compare_experiments(original, changed, allow_changes=["rubric_version"])
    assert comparison["allowed_changes"]["rubric_version"]["after"] == "v3"
    changed.cases[0].verdict = "failed"
    assert compare_experiments(original, changed, allow_changes=["rubric_version"])[
        "regressed"
    ] == [changed.cases[0].case_id]

    incomplete = original.model_copy(deep=True)
    del incomplete.manifest["git_commit"]
    with pytest.raises(ValueError, match="provenance"):
        compare_experiments(incomplete, incomplete)
    environment = original.model_copy(deep=True)
    environment.manifest["runtime"] = {"python": "different"}
    assert "runtime" in compare_experiments(original, environment)["environment_changes"]


def test_git_provenance_tracks_uncommitted_and_untracked_changes(repo):
    clean = git_provenance(repo)
    (repo / "test.txt").write_text("changed")
    dirty = git_provenance(repo)
    assert dirty["git_commit"] == clean["git_commit"]
    assert dirty["dirty"] is True
    assert dirty["working_tree_sha256"] != clean["working_tree_sha256"]
    (repo / "new.py").write_text("untracked code")
    assert git_provenance(repo)["working_tree_sha256"] != dirty["working_tree_sha256"]


def test_checks_distinguish_missing_null_bool_and_invalid_paths():
    checks = [
        Check(path=["missing"], expected=None),
        Check(path=["a"], expected=1),
        Check(path=["b", -1], operation="exists"),
    ]
    assert all(not r["passed"] for r in check_output({"a": True, "b": [1]}, checks))


def test_schema_cli_includes_new_contracts(capsys):
    assert main(["schemas"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"EvaluationRequest", "FeedbackResult", "Dataset"} <= result.keys()
