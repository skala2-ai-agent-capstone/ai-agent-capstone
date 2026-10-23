"""python -m ai_agents.cli.quality {check,run,review-template,report,compare}."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from ..validation.models import Dataset, ExperimentResult
from ..validation.quality import QualityConfig, check_quality_dataset, run_quality
from ..validation.quality_reviews import (
    DIMENSIONS,
    LABELS,
    ReviewSheet,
    compare_quality,
    export_quality_report,
    review_template,
)
from ..validation.runner import write_json


def parser():
    root = argparse.ArgumentParser(description="MCP 없는 실제 LLM 품질 실행·사람 검토·비교")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--dataset", type=Path, required=True)
        if name == "run":
            cmd.add_argument("--output-dir", type=Path, required=True)
            cmd.add_argument("--label", required=True)
            cmd.add_argument("--langsmith", action="store_true")
    for name in ("review-template", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("experiment", type=Path)
        if name == "report":
            cmd.add_argument("--reviews", type=Path)
            cmd.add_argument("--output-dir", type=Path, required=True)
        else:
            cmd.add_argument("--output", type=Path, required=True)
    cmd = sub.add_parser("compare")
    cmd.add_argument("before", type=Path)
    cmd.add_argument("after", type=Path)
    cmd.add_argument("--before-reviews", type=Path)
    cmd.add_argument("--after-reviews", type=Path)
    cmd.add_argument("--output-dir", type=Path, required=True)
    cmd.add_argument("--allow-change", action="append", default=[],
                     choices=["model", "model_settings", "prompt_version", "rubric_version"])
    return root


def read_experiment(path):
    return ExperimentResult.model_validate_json(path.read_text())


def read_reviews(path):
    return ReviewSheet.model_validate_json(path.read_text()) if path else None


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"run", "check"}:
            dataset = Dataset.model_validate_json(args.dataset.read_text())
            output = check_quality_dataset(dataset)
            if args.command == "run":
                load_dotenv(Path.cwd() / ".env", override=False)
                config = QualityConfig(
                    model=os.getenv("QUALITY_LLM_MODEL", ""),
                    base_url=os.getenv("QUALITY_LLM_BASE_URL", ""),
                    model_revision=os.getenv("QUALITY_MODEL_REVISION") or "unspecified",
                )
                experiment = asyncio.run(run_quality(
                    dataset, config, api_key=os.getenv("QUALITY_LLM_API_KEY"),
                    output_dir=args.output_dir, repo=Path.cwd(), label=args.label,
                    langsmith=args.langsmith,
                ))
                write_json(args.output_dir / "reviews.json", review_template(experiment).model_dump(
                    mode="json",
                ))
                output = {
                    "experiment_id": experiment.manifest["experiment_id"],
                    "cases": len(experiment.cases),
                    "generated": sum(c.verdict == "passed" for c in experiment.cases),
                    "failed": sum(c.verdict == "failed" for c in experiment.cases),
                    "quality_status": "pending_human_review",
                }
        elif args.command == "review-template":
            sheet = review_template(read_experiment(args.experiment))
            write_json(args.output, sheet.model_dump(mode="json"))
            output = {"status": "review_template_written", "cases": len(sheet.cases)}
        elif args.command == "report":
            output = export_quality_report(
                read_experiment(args.experiment), read_reviews(args.reviews), args.output_dir,
            )
        else:
            output = compare_quality(
                read_experiment(args.before), read_experiment(args.after),
                read_reviews(args.before_reviews), read_reviews(args.after_reviews),
                allow_changes=args.allow_change,
            )
            args.output_dir.mkdir(parents=True, exist_ok=False)
            write_json(args.output_dir / "comparison.json", output)
            lines = ["# LLM 품질 비교", "",
                     f"실행 모드: {output['after']['execution_mode']}",
                     "같은 평가셋에서 양쪽 모두 사람이 검토한 사례만 품질 평균을 비교한다.",
                     f"동일 검토 사례 수: {len(output['paired_reviewed_case_ids'])}", "",
                     "| 품질 항목 | 변경 전 | 변경 후 |", "| --- | ---: | ---: |"]
            for key in DIMENSIONS:
                values = output["paired_quality_mean"][key]
                left = values["before"] if values["before"] is not None else "미검토"
                right = values["after"] if values["after"] is not None else "미검토"
                lines.append(f"| {LABELS[key]} | {left} | {right} |")
            lines += ["", f"자동 생성·검증 성공: {output['before']['generated']} → "
                      f"{output['after']['generated']} / {output['after']['total']}",
                      f"검토자 변경 사례 수: {len(output['different_reviewers'])}",
                      "코드·설정·실행 환경 차이는 comparison.json에서 확인한다.",
                      "test_transport는 실행기 시험이며 실제 모델 품질 비교가 아니다.", ""]
            (args.output_dir / "comparison.md").write_text("\n".join(lines))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1 if args.command == "run" and output["failed"] else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "quality_validation_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
