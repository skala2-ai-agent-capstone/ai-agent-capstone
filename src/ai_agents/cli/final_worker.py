"""python -m ai_agents.cli.final_worker {schemas,worker}."""

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..integrations.final_rabbit import consume_final_evaluations
from ..orchestration.final_worker import FinalEvaluationWorker, FinalJobExecutor
from ..shared.final_job_contracts import FinalEvaluationEvent, FinalEvaluationSaved
from ..validation.runner import git_provenance
from .final_evaluation import required


def parser():
    root = argparse.ArgumentParser(description="최종 평가 작업 수신·MCP 저장·완료 알림")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    worker = sub.add_parser("worker")
    worker.add_argument("--token-provider", required=True,
                        help="신뢰하는 로컬 async 함수 module:function, event → SecretStr")
    worker.add_argument("--checkpoint-dir", type=Path, default=Path("outputs/final-checkpoints"))
    worker.add_argument("--trace-dir", type=Path, default=Path("outputs/final-worker-traces"))
    worker.add_argument("--max-retries", type=int, choices=range(11), default=3)
    worker.add_argument("--langsmith", action="store_true")
    return root


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    module, name = args.token_provider.split(":", 1)
    provider = getattr(importlib.import_module(module), name)
    if not callable(provider):
        raise ValueError("token provider must be callable")
    model, base_url = required("FINAL_LLM_MODEL"), required("FINAL_LLM_BASE_URL")
    business_url, rag_url = required("FINAL_BUSINESS_MCP_URL"), required("FINAL_RAG_MCP_URL")
    amqp_url, code = required("FINAL_AMQP_URL"), git_provenance(Path.cwd())
    async with AsyncOpenAI(
        base_url=base_url, api_key=os.getenv("FINAL_LLM_API_KEY") or "self-hosted",
        timeout=120, max_retries=0,
    ) as client:
        executor = FinalJobExecutor(
            provider, client, model=model, business_url=business_url, rag_url=rag_url,
            checkpoint_dir=args.checkpoint_dir, code_provenance=code,
        )
        worker = FinalEvaluationWorker(
            executor, trace_dir=args.trace_dir, model=model, code_provenance=code,
            langsmith=args.langsmith, max_retries=args.max_retries,
        )
        await consume_final_evaluations(amqp_url, worker)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schemas":
            print(json.dumps({
                model.__name__: model.model_json_schema()
                for model in (FinalEvaluationEvent, FinalEvaluationSaved)
            }, ensure_ascii=False, indent=2))
        else:
            asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "worker_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
