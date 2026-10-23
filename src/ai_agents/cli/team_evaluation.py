"""python -m ai_agents.cli.team_evaluation {check,schemas,run,save-request}."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..evaluation_agent.team.agent import PROMPT_VERSION, SYSTEM_PROMPT, TeamAgent
from ..evaluation_agent.team.report import ACTIONS, metrics_for
from ..observability import TraceSession
from ..orchestration.team_evaluation import build_save_request
from ..shared.team_contracts import TeamReport, TeamSaveReceipt, TeamSaveRequest, TeamSnapshot
from ..validation.quality import QualityConfig
from ..validation.runner import digest, git_provenance, write_json


def parser():
    root = argparse.ArgumentParser(description="공개된 팀 집계 → 팀 코칭 리포트")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    for name in ("check", "run", "save-request"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--snapshot", type=Path, required=True)
        if name == "run":
            cmd.add_argument("--output-dir", type=Path, required=True)
            cmd.add_argument("--langsmith", action="store_true")
        if name == "save-request":
            cmd.add_argument("--report", type=Path, required=True)
            cmd.add_argument("--output", type=Path, required=True)
    return root


async def run(args, snapshot):
    load_dotenv(Path.cwd() / ".env", override=False)
    config = None
    if metrics_for(snapshot):
        config = QualityConfig(model=os.getenv("TEAM_LLM_MODEL", ""),
                               base_url=os.getenv("TEAM_LLM_BASE_URL", ""),
                               model_revision=os.getenv("TEAM_MODEL_REVISION") or "unspecified")
    provenance = git_provenance(Path.cwd())
    args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    with TraceSession(args.output_dir / "traces", langsmith=args.langsmith, metadata={
        "purpose": "team", "request_id": str(snapshot.request_id),
        "model": config.model if config else "not_called",
        "prompt_version": PROMPT_VERSION, "git_commit": provenance["git_commit"],
    }) as session:
        if config:
            async with AsyncOpenAI(base_url=config.base_url,
                                   api_key=os.getenv("TEAM_LLM_API_KEY") or "self-hosted",
                                   timeout=config.timeout_seconds, max_retries=0) as client:
                report = await TeamAgent(client, config.model).generate(snapshot)
        else:
            report = await TeamAgent(None, "not_called").generate(snapshot)
        write_json(args.output_dir / "snapshot.json", snapshot.model_dump(mode="json"))
        write_json(args.output_dir / "report.json", report.model_dump(mode="json"))
        write_json(args.output_dir / "save-request.json",
                   build_save_request(snapshot, report).model_dump(mode="json"))
    write_json(args.output_dir / "manifest.json", {
        **provenance, "model": config.model if config else None,
        "model_revision": config.model_revision if config else None,
        "prompt_version": PROMPT_VERSION, "prompt_sha256": digest(SYSTEM_PROMPT),
        "coaching_catalog_sha256": digest(ACTIONS), "trace_id": session.trace_id,
        "model_settings": {"endpoint_sha256": digest(config.base_url),
                           "temperature": 0, "max_tokens": 1800, "sdk_retries": 0,
                           "validation_retries": 1, "timeout_seconds": config.timeout_seconds,
                           "response_format": "json_object"} if config else None,
        "langsmith_requested": args.langsmith, "langsmith_export_failed": session.telemetry_failed,
        "status": report.status, "persistence": "local_only",
    })
    return {"status": report.status, "persistence": "local_only", "trace_id": session.trace_id}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schemas":
            result = {model.__name__: model.model_json_schema() for model in (
                TeamSnapshot, TeamReport, TeamSaveRequest, TeamSaveReceipt,
            )}
        else:
            snapshot = TeamSnapshot.model_validate_json(args.snapshot.read_text())
            if args.command == "check":
                result = {"status": "valid", "groups": len(snapshot.groups), "network_calls": 0}
            elif args.command == "save-request":
                report = TeamReport.model_validate_json(args.report.read_text())
                request = build_save_request(snapshot, report)
                write_json(args.output, request.model_dump(mode="json"))
                result = {"status": "exported", "network_calls": 0}
            else:
                result = asyncio.run(run(args, snapshot))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "team_evaluation_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
