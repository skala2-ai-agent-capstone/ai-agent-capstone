"""저장된 평가 기준을 사용하는 최종 평가 1건 실행. 백엔드·DB 직접 호출 없음."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..integrations.final_mcp import FinalMCPGateway
from ..observability import TraceSession
from ..orchestration.final_evaluation import FinalEvaluationJob, FinalEvaluationWorkflow
from ..validation.runner import git_provenance


def parser():
    root = argparse.ArgumentParser(description="저장 기준 조회 → 최종 평가 → MCP 초안·코칭 저장")
    root.add_argument("--evaluation-id", required=True)
    root.add_argument("--operation-id", required=True, help="같은 실행 재시도에서는 같은 ID 사용")
    root.add_argument("--checkpoint-dir", type=Path, default=Path("outputs/final-checkpoints"))
    root.add_argument("--trace-dir", type=Path, default=Path("outputs/final-traces"))
    root.add_argument("--langsmith", action="store_true")
    return root


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    model, base_url = required("FINAL_LLM_MODEL"), required("FINAL_LLM_BASE_URL")
    token = required("FINAL_MCP_TOKEN")  # 백엔드에서 발급한 해당 평가 건 전용 evaluation 토큰
    job = FinalEvaluationJob(evaluation_id=args.evaluation_id, operation_id=args.operation_id)
    code = git_provenance(Path.cwd())
    with TraceSession(
        args.trace_dir,
        langsmith=args.langsmith,
        metadata={
            "purpose": "final",
            "model": model,
            "git_commit": code["git_commit"],
        },
    ):
        async with FinalMCPGateway(
            token,
            business_url=required("FINAL_BUSINESS_MCP_URL"),
            rag_url=required("FINAL_RAG_MCP_URL"),
        ) as gateway:
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=os.getenv("FINAL_LLM_API_KEY") or "self-hosted",
                timeout=120,
                max_retries=0,
            ) as client:
                workflow = FinalEvaluationWorkflow(
                    gateway,
                    client,
                    model=model,
                    checkpoint_dir=args.checkpoint_dir,
                    code_provenance=code,
                )
                return await workflow.run(job)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # 원문·인증 헤더·모델 출력이 예외 문장에 섞일 수 있어 종류만 출력한다.
        print(json.dumps({"status": "final_evaluation_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
