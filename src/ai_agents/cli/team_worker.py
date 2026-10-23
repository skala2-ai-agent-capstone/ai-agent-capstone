"""python -m ai_agents.cli.team_worker {schemas,check-event,worker}.

팀 평가(팀 코칭 리포트) Worker. 백엔드가 team_evaluation.requests로 보낸 TEAM_EVALUATE를 받아
business-mcp에서 고정 스냅샷을 읽고 리포트를 만들어 저장한다. team 토큰은 요청마다 백엔드가
/internal/team-runs로 발급해 토큰 파일(--token-file, request_id별)에 둔다.
Worker는 내부 API를 부르지 않는다.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..evaluation_agent.team.agent import TeamAgent
from ..integrations.final_tokens import FileFinalTokenProvider
from ..integrations.team_rabbit import consume_team_evaluations
from ..orchestration.team_worker import TeamJobExecutor, TeamWorker
from ..shared.team_job_contracts import TeamEvaluateEvent
from ..validation.runner import git_provenance


def parser():
    root = argparse.ArgumentParser(description="팀 평가 요청 수신·리포트 생성·MCP 저장")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    event = sub.add_parser("check-event", help="큐 메시지 파일 형식만 검사. 실행·저장 없음")
    event.add_argument("--event", type=Path, required=True)
    worker = sub.add_parser("worker")
    worker.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="백엔드가 발급한 request_id별 token/expires_at JSON 파일",
    )
    worker.add_argument("--checkpoint-dir", type=Path, default=Path("outputs/team-checkpoints"))
    worker.add_argument("--trace-dir", type=Path, default=Path("outputs/team-worker-traces"))
    worker.add_argument("--max-retries", type=int, choices=range(11), default=3)
    worker.add_argument("--langsmith", action="store_true")
    return root


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    provider = FileFinalTokenProvider(args.token_file, key="request_id")
    provider.read()  # 연결/소비 전에 파일 형식을 검사한다.
    business_url = required("TEAM_BUSINESS_MCP_URL").rstrip("/")
    if not business_url.endswith("/mcp"):
        raise ValueError("TEAM_BUSINESS_MCP_URL must end with /mcp")
    model, base_url = required("TEAM_LLM_MODEL"), required("TEAM_LLM_BASE_URL")
    amqp_url, code = required("TEAM_AMQP_URL"), git_provenance(Path.cwd())
    async with AsyncOpenAI(
        base_url=base_url,
        api_key=os.getenv("TEAM_LLM_API_KEY") or "self-hosted",
        timeout=120,
        max_retries=0,
    ) as client:
        executor = TeamJobExecutor(
            provider,
            TeamAgent(client, model),
            business_url=business_url,
            checkpoint_dir=args.checkpoint_dir,
        )
        worker = TeamWorker(
            executor,
            trace_dir=args.trace_dir,
            model=model,
            code_provenance=code,
            langsmith=args.langsmith,
            max_retries=args.max_retries,
        )
        await consume_team_evaluations(amqp_url, worker)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schemas":
            print(
                json.dumps(
                    {"TeamEvaluateEvent": TeamEvaluateEvent.model_json_schema()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "check-event":
            event = TeamEvaluateEvent.model_validate_json(args.event.read_bytes())
            print(
                json.dumps(
                    {"status": "valid", "request_id": str(event.request_id), "network_calls": 0}
                )
            )
        else:
            asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "team_worker_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
