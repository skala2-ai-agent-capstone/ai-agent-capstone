"""python -m ai_agents.cli.validation {schemas,run,compare}."""

import argparse
import asyncio
import importlib
import json
from pathlib import Path

from ..shared.execution_contracts import EvaluationRequest, FeedbackResult
from ..validation.models import Dataset, ExperimentResult
from ..validation.runner import compare_experiments, run_experiment, write_json


def parser():
    root = argparse.ArgumentParser(description="고정 평가셋 실행·검증결과서 기록·전후 비교")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas", help="신규 실행·피드백·평가셋 JSON Schema 출력")
    run = sub.add_parser("run")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--target", required=True, help="신뢰하는 로컬 Python 함수 module:function")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument("--label", required=True)
    for name in ("model", "prompt-version", "rubric-version", "data-version"):
        run.add_argument(f"--{name}", required=True)
    run.add_argument("--model-settings", default="{}", help="실제 target에 적용한 생성 설정 JSON")
    run.add_argument("--langsmith", action="store_true")
    compare = sub.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument(
        "--allow-change",
        action="append",
        default=[],
        choices=["model", "model_settings", "prompt_version", "rubric_version"],
    )
    compare.add_argument("--output", type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schemas":
            output = {
                model.__name__: model.model_json_schema()
                for model in (EvaluationRequest, FeedbackResult, Dataset)
            }
        elif args.command == "compare":
            output = compare_experiments(
                ExperimentResult.model_validate_json(args.before.read_text()),
                ExperimentResult.model_validate_json(args.after.read_text()),
                allow_changes=args.allow_change,
            )
            if args.output:
                write_json(args.output, output)
        else:
            dataset = Dataset.model_validate_json(args.dataset.read_text())
            module, name = args.target.split(":", 1)
            target = getattr(importlib.import_module(module), name)
            if not callable(target):
                raise ValueError("target must be callable")
            settings = json.loads(args.model_settings)
            if not isinstance(settings, dict):
                raise ValueError("model settings must be an object")
            result = asyncio.run(
                run_experiment(
                    dataset,
                    target,
                    output_dir=args.output_dir,
                    repo=args.repo.resolve(),
                    label=args.label,
                    target_name=args.target,
                    model=args.model,
                    prompt_version=args.prompt_version,
                    rubric_version=args.rubric_version,
                    data_version=args.data_version,
                    model_settings=settings,
                    langsmith=args.langsmith,
                )
            )
            output = {
                "experiment_id": result.manifest["experiment_id"],
                "cases": len(result.cases),
                "output_dir": str(args.output_dir),
                "passed": sum(c.verdict == "passed" for c in result.cases),
                "failed": sum(c.verdict == "failed" for c in result.cases),
                "unreviewed": sum(c.verdict == "unreviewed" for c in result.cases),
                "dirty": result.manifest["dirty"],
            }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1 if args.command == "run" and output["failed"] else 0
    except Exception as exc:
        # 모델/도구 예외·Pydantic 오류 본문에는 입력 원문이 포함될 수 있다.
        print(json.dumps({"error_type": type(exc).__name__, "status": "validation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
