import csv
import inspect
import json
import platform
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from ..observability import TraceSession
from .models import CaseResult, Dataset, ExperimentResult


def digest(value) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def runtime_provenance() -> dict:
    packages = {}
    for name in ("langgraph", "langsmith", "mcp", "openai", "pydantic"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "packages": packages}


def git_provenance(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

    commit = git("rev-parse", "HEAD").decode().strip()
    tracked = git("diff", "HEAD", "--binary")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    # 경로·원문을 보고서에 쓰지 않고 미추적 파일까지 작업 트리 해시에 포함한다.
    tree = sha256(tracked)
    for name in sorted(n for n in untracked if n):
        path = root / name.decode()
        tree.update(name)
        if path.is_symlink():
            tree.update(str(path.readlink()).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    tree.update(block)
    lock = root / "uv.lock"
    return {
        "git_commit": commit,
        "dirty": bool(tracked or any(untracked)),
        "working_tree_sha256": tree.hexdigest(),
        "dependency_lock_sha256": sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
    }


def check_output(actual: dict, checks) -> list[dict]:
    results = []
    for check in checks:
        found, value = True, actual
        try:
            for key in check.path:
                if isinstance(value, list) and (type(key) is not int or key < 0):
                    raise KeyError(key)
                if isinstance(value, dict) and not isinstance(key, str):
                    raise KeyError(key)
                value = value[key]
        except (KeyError, IndexError, TypeError):
            found, value = False, None
        if check.operation == "exists":
            passed = found
        elif check.operation == "equals":
            passed = found and type(value) is type(check.expected) and value == check.expected
        else:
            passed = found and isinstance(value, (str, list, dict))
            try:
                passed = passed and check.expected in value
            except TypeError:
                passed = False
        results.append(
            {
                "path": check.path,
                "operation": check.operation,
                "expected": check.expected,
                "passed": bool(passed),
            }
        )
    return results


async def run_experiment(
    dataset: Dataset,
    target,
    *,
    output_dir: Path,
    repo: Path,
    label: str,
    target_name: str,
    model: str,
    prompt_version: str,
    rubric_version: str,
    data_version: str,
    model_settings: dict | None = None,
    langsmith: bool = False,
) -> ExperimentResult:
    provenance = git_provenance(repo)
    # 출력을 만들기 전에 데이터셋 해시를 고정하고, 기대값은 target에 전달하지 않는다.
    dataset_hash = digest(dataset.model_dump(mode="json"))
    output_dir.mkdir(parents=True, exist_ok=False)
    experiment_id = str(uuid4())
    manifest = {
        "experiment_id": experiment_id,
        "label": label,
        "target": target_name,
        "purpose": dataset.purpose,
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_sha256": dataset_hash,
        "model": model,
        "model_settings": model_settings or {},
        "prompt_version": prompt_version,
        "rubric_version": rubric_version,
        "data_version": data_version,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "langsmith_requested": langsmith,
        "runtime": runtime_provenance(),
        **provenance,
    }
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "dataset.json", dataset.model_dump(mode="json"))
    cases = []
    export_failed = False
    with (output_dir / "results.jsonl").open("x", encoding="utf-8") as partial:
        for example in dataset.examples:
            started = perf_counter()
            with TraceSession(
                output_dir / "traces",
                metadata={
                    "experiment_id": experiment_id,
                    "request_id": example.case_id,
                    "purpose": dataset.purpose,
                    "model": model,
                    "prompt_version": prompt_version,
                    "rubric_version": rubric_version,
                    "dataset_version": dataset.version,
                    "git_commit": provenance["git_commit"],
                },
                langsmith=langsmith,
            ) as session:
                try:
                    with session.span("validation_case", inputs=example.inputs) as span:
                        # 복사본 전달: target이 입력을 수정해도 평가셋/다음 실행은 변하지 않는다.
                        output = target(json.loads(json.dumps(example.inputs)))
                        if inspect.isawaitable(output):
                            output = await output
                        if hasattr(output, "model_dump"):
                            output = output.model_dump(mode="json")
                        output = json.loads(json.dumps(output, allow_nan=False))
                        actual = {"status": "ok", "output": output}
                        span.result(actual)
                except Exception as exc:
                    actual = {"status": "error", "error_type": type(exc).__name__}
            export_failed = export_failed or session.telemetry_failed
            checked = check_output(actual, example.checks)
            verdict = (
                "unreviewed"
                if not checked
                else "passed"
                if all(c["passed"] for c in checked)
                else "failed"
            )
            result = CaseResult(
                case_id=example.case_id,
                category=example.category,
                verdict=verdict,
                expected_description=example.expected_description,
                evidence_ids=example.evidence_ids,
                actual=actual,
                checks=checked,
                duration_ms=round((perf_counter() - started) * 1000, 3),
                trace_id=session.trace_id,
            )
            cases.append(result)
            partial.write(result.model_dump_json() + "\n")
            partial.flush()
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["langsmith_export_failed"] = export_failed
    result = ExperimentResult(manifest=manifest, cases=cases)
    write_json(output_dir / "experiment.json", result.model_dump(mode="json"))
    with (output_dir / "report.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "검증 ID",
                "유형",
                "입력",
                "기대 결과",
                "근거",
                "실제 결과",
                "판정",
                "조치",
                "trace_id",
            ]
        )
        for example, case in zip(dataset.examples, cases, strict=True):
            values = [
                case.case_id,
                case.category,
                json.dumps(example.inputs, ensure_ascii=False),
                case.expected_description,
                ", ".join(case.evidence_ids),
                json.dumps(case.actual, ensure_ascii=False),
                case.verdict,
                "",
                case.trace_id,
            ]
            # CSV를 스프레드시트로 열 때 수식으로 해석되지 않게 한다.
            writer.writerow(["'" + v if v.startswith(("=", "+", "-", "@")) else v for v in values])
    return result


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)


def compare_experiments(before: ExperimentResult, after: ExperimentResult, *, allow_changes=()):
    fixed = {"purpose", "dataset_id", "dataset_version", "dataset_sha256", "data_version"}
    configurable = {"model", "model_settings", "prompt_version", "rubric_version"}
    required = fixed | configurable | {"git_commit", "dirty", "working_tree_sha256", "target"}
    for experiment in (before, after):
        if not required.issubset(experiment.manifest):
            raise ValueError("experiment provenance is incomplete")
    if set(allow_changes) - configurable:
        raise ValueError("only model/model_settings/prompt_version/rubric_version may differ")
    for field in fixed | (configurable - set(allow_changes)):
        if before.manifest.get(field) != after.manifest.get(field):
            raise ValueError(f"incompatible experiments: {field}")
    left = {c.case_id: c for c in before.cases}
    right = {c.case_id: c for c in after.cases}
    if (
        len(left) != len(before.cases)
        or len(right) != len(after.cases)
        or left.keys() != right.keys()
    ):
        raise ValueError("case IDs must be unique and identical")
    if not left:
        raise ValueError("empty experiments cannot be compared")

    def summary(cases):
        return {
            status: sum(c.verdict == status for c in cases)
            for status in ("passed", "failed", "unreviewed")
        }

    return {
        "provenance": {
            side: {
                key: experiment.manifest.get(key)
                for key in ("experiment_id", "git_commit", "working_tree_sha256", "target")
            }
            for side, experiment in (("before", before), ("after", after))
        },
        "before": summary(before.cases),
        "after": summary(after.cases),
        "total": len(left),
        "before_pass_rate": sum(c.verdict == "passed" for c in before.cases) / len(left),
        "after_pass_rate": sum(c.verdict == "passed" for c in after.cases) / len(left),
        "improved": [
            k for k in left if left[k].verdict == "failed" and right[k].verdict == "passed"
        ],
        "regressed": [
            k for k in left if left[k].verdict == "passed" and right[k].verdict == "failed"
        ],
        "review_changed": [
            k
            for k in left
            if left[k].verdict != right[k].verdict
            and "unreviewed" in (left[k].verdict, right[k].verdict)
        ],
        "allowed_changes": {
            k: {"before": before.manifest.get(k), "after": after.manifest.get(k)}
            for k in allow_changes
        },
        "dirty_warning": bool(before.manifest.get("dirty") or after.manifest.get("dirty")),
        "environment_changes": {
            key: {"before": before.manifest.get(key), "after": after.manifest.get(key)}
            for key in ("dependency_lock_sha256", "runtime")
            if before.manifest.get(key) != after.manifest.get(key)
        },
    }
