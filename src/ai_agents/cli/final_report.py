"""python -m ai_agents.cli.final_report {schemas,export}. 원문은 명시한 0600 파일에만 출력."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from ..evaluation_agent.final.page import build_report_save_request
from ..orchestration.final_evaluation import PreparedFinalEvaluation
from ..shared.final_page_contracts import FinalEvaluationPage, FinalReportSaveRequest


def parser():
    root = argparse.ArgumentParser(description="검증된 체크포인트 → 개인 평가 페이지 JSON")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    export = sub.add_parser("export")
    export.add_argument("--checkpoint", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--save-request", action="store_true", help="제안 MCP 저장 인자까지 출력")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schemas":
            print(json.dumps({
                model.__name__: model.model_json_schema()
                for model in (FinalEvaluationPage, FinalReportSaveRequest)
            }, ensure_ascii=False, indent=2))
            return 0
        prepared = PreparedFinalEvaluation.model_validate_json(args.checkpoint.read_text())
        request = build_report_save_request(prepared)
        output = request if args.save_request else request.report
        encoded = output.model_dump_json(indent=2)
        # 기존 파일·체크포인트·symlink를 덮어쓰지 않는다. 부모 폴더는 명시적으로 준비한다.
        fd, temporary = tempfile.mkstemp(dir=args.output.parent, prefix=".final-page-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, args.output)  # 같은 파일시스템에서 원자적 공개, 덮어쓰기 불가.
        finally:
            os.unlink(temporary)
        print(json.dumps({"status": "exported", "report_sha256": request.report_sha256}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "report_export_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
