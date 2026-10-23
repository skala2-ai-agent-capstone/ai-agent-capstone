"""python -m ai_agents.cli.rubric_worker {schemas,check-config,check-event,once,worker}.

내규 업로드 뒤 평가 기준 초안 생성. 토큰은 business-mcp /internal/rubric-runs로 회사마다 발급한
kind=rubric 실행 토큰(RUBRIC_MCP_TOKEN)이다. Worker는 내부 API를 직접 부르지 않는다.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..criteria_agent.builder import OpenAIRubricBuilder
from ..integrations.rubric_mcp import RubricMCPGateway, token_claims
from ..integrations.rubric_rabbit import consume_rubric_generation
from ..orchestration.rubric_worker import RubricWorker, generate_drafts
from ..shared.rubric_generation_contracts import RubricGenerationEvent, RubricGenerationResult
from ..validation.runner import git_provenance

REQUIRED = ("RUBRIC_AMQP_URL", "RUBRIC_BUSINESS_MCP_URL", "RUBRIC_RAG_MCP_URL", "RUBRIC_MCP_TOKEN")


def parser():
    root = argparse.ArgumentParser(description="내규 업로드 뒤 평가 기준 초안 생성 Worker")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    sub.add_parser("check-config", help="환경변수 형식과 토큰 종류·회사·만료만 검사. 연결 없음")
    event = sub.add_parser("check-event", help="큐 메시지 파일 형식만 검사. 실행·저장 없음")
    event.add_argument("--event", type=Path, required=True)
    for name in ("once", "worker"):
        command = sub.add_parser(name)
        command.add_argument("--trace-dir", type=Path, default=Path("outputs/rubric-traces"))
        command.add_argument("--langsmith", action="store_true")
        command.add_argument("--repo", type=Path, default=Path.cwd())
        if name == "once":
            command.add_argument("--event", type=Path, required=True)
    return root


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def mcp_url(name):
    value = required(name).rstrip("/")
    if not value.endswith("/mcp"):
        raise ValueError(f"{name} must end with /mcp")
    return value


def worker_company(token):
    """토큰이 평가 기준 작성용(kind=rubric)이고 만료 전인지 확인하고 회사 ID를 돌려준다."""
    claims = token_claims(token)
    if claims.get("kind") != "rubric" or not isinstance(claims.get("company_id"), str):
        raise ValueError("RUBRIC_MCP_TOKEN must be a kind=rubric token from /internal/rubric-runs")
    if claims.get("exp", 0) < time.time():
        raise ValueError("RUBRIC_MCP_TOKEN is expired")
    return claims["company_id"]


def llm_model():
    return os.getenv("RUBRIC_LLM_MODEL", "").strip() or os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def check_config():
    load_dotenv(Path.cwd() / ".env", override=False)
    missing = [name for name in REQUIRED if not os.getenv(name, "").strip()]
    if not (os.getenv("RUBRIC_LLM_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()):
        missing.append("RUBRIC_LLM_API_KEY")
    invalid = []
    for name, schemes in (
        ("RUBRIC_AMQP_URL", {"amqp", "amqps"}),
        ("RUBRIC_BUSINESS_MCP_URL", {"http", "https"}),
        ("RUBRIC_RAG_MCP_URL", {"http", "https"}),
        ("RUBRIC_LLM_BASE_URL", {"http", "https"}),
    ):
        if name in missing or not os.getenv(name, "").strip():
            continue
        try:
            url = urlsplit(os.environ[name])
            _ = url.port
            if (url.scheme not in schemes or not url.hostname
                    or any(c in url.hostname for c in "<> ") or url.fragment):
                raise ValueError("invalid URL")
            if name != "RUBRIC_AMQP_URL" and (url.username or url.password or url.query):
                raise ValueError("credentials must be separate")
            if name.endswith("_MCP_URL") and not url.path.rstrip("/").endswith("/mcp"):
                raise ValueError("MCP URL must end with /mcp")
        except ValueError:
            invalid.append(name)
    company = None
    if "RUBRIC_MCP_TOKEN" not in missing:
        try:
            company = worker_company(os.environ["RUBRIC_MCP_TOKEN"])
        except ValueError:
            invalid.append("RUBRIC_MCP_TOKEN")
    return {"status": "ready" if not missing and not invalid else "incomplete",
            "missing": missing, "invalid": invalid, "company_id": company, "network_calls": 0}


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    token = required("RUBRIC_MCP_TOKEN")
    company = worker_company(token)
    business, rag = mcp_url("RUBRIC_BUSINESS_MCP_URL"), mcp_url("RUBRIC_RAG_MCP_URL")
    model = llm_model()
    api_key = os.getenv("RUBRIC_LLM_API_KEY", "").strip() or required("OPENAI_API_KEY")
    code = git_provenance(args.repo.resolve())
    # RUBRIC_LLM_BASE_URL을 비우면 OpenAI로 바로 보낸다.
    # 기록 프록시를 거치려면 그 /v1 주소를 준다(X-Stage: rubric).
    async with AsyncOpenAI(
        base_url=os.getenv("RUBRIC_LLM_BASE_URL", "").strip() or None,
        api_key=api_key, timeout=120, max_retries=1,
    ) as client:
        builder = OpenAIRubricBuilder(client, model)

        async def execute(event):
            async with RubricMCPGateway(token, business_url=business, rag_url=rag) as gateway:
                return await generate_drafts(gateway, builder, event)

        worker = RubricWorker(
            execute, company_id=company, trace_dir=args.trace_dir, model=model,
            code_provenance=code, langsmith=args.langsmith,
        )
        if args.command == "worker":
            await consume_rubric_generation(required("RUBRIC_AMQP_URL"), worker)
            return 0

        # 로컬 1건 확인 모드. MCP에는 저장하지만 RabbitMQ에 발행/ack하지 않고 할 일만 출력한다.
        async def publish(queue, payload, error_code):
            print(json.dumps({"would_publish": queue, "error_code": error_code},
                             ensure_ascii=False))

        async def ack():
            pass

        result = await worker.handle(args.event.read_bytes(), publish=publish, ack=ack)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "완료" else 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "check-config":
            result = check_config()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "ready" else 2
        if args.command == "check-event":
            RubricGenerationEvent.model_validate_json(args.event.read_bytes())
            print(json.dumps({"status": "valid", "type": "RUBRIC_GENERATE", "network_calls": 0}))
            return 0
        if args.command == "schemas":
            models = (RubricGenerationEvent, RubricGenerationResult)
            print(json.dumps({m.__name__: m.model_json_schema() for m in models},
                             ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "rubric_worker_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
