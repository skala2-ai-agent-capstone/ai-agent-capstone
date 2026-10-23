"""python -m ai_agents.cli.feedback {schemas,once,worker}."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..evaluation_agent.feedback.agent import FeedbackAgent
from ..integrations.feedback_rabbit import consume_feedback
from ..integrations.live_mcp import LiveMCPGateway
from ..orchestration.feedback_worker import FeedbackWorker
from ..orchestration.live_feedback import LiveFeedbackWorkflow
from ..shared.live_feedback_contracts import (
    GeneratedFeedback,
    LiveEvaluationContext,
    LiveEvaluationEvent,
    MessageBatch,
    SaveReceipt,
)
from ..validation.runner import git_provenance


def parser():
    root = argparse.ArgumentParser(description="최근 질문·응답 5회 중간 피드백")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("schemas")
    sub.add_parser("check-config", help="연동 환경변수 형식만 검사. 연결·토큰 발급 없음")
    event = sub.add_parser("check-event", help="큐 메시지 파일 형식만 검사. 실행·저장 없음")
    event.add_argument("--event", type=Path, required=True)
    for name in ("once", "worker"):
        command = sub.add_parser(name)
        command.add_argument("--trace-dir", type=Path, default=Path("outputs/feedback-traces"))
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


def check_config():
    load_dotenv(Path.cwd() / ".env", override=False)
    names = (
        "FEEDBACK_AMQP_URL", "FEEDBACK_MCP_URL", "FEEDBACK_MCP_TOKEN",
        "FEEDBACK_LLM_BASE_URL", "FEEDBACK_LLM_MODEL", "FEEDBACK_LLM_API_KEY",
    )
    missing = [name for name in names if not os.getenv(name, "").strip()]
    invalid = []
    for name, schemes in (
        ("FEEDBACK_AMQP_URL", {"amqp", "amqps"}),
        ("FEEDBACK_MCP_URL", {"http", "https"}),
        ("FEEDBACK_LLM_BASE_URL", {"http", "https"}),
    ):
        if name in missing:
            continue
        try:
            url = urlsplit(os.environ[name])
            _ = url.port
            if (url.scheme not in schemes or not url.hostname
                    or any(c in url.hostname for c in "<> ") or url.fragment):
                raise ValueError("invalid URL")
            if name != "FEEDBACK_AMQP_URL" and (url.username or url.password or url.query):
                raise ValueError("credentials must be separate")
        except ValueError:
            invalid.append(name)
    return {"status": "ready" if not missing and not invalid else "incomplete",
            "missing": missing, "invalid": invalid, "network_calls": 0}


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    # 기본 OpenAI 주소로 원문이 나가지 않도록 피드백 모델 주소를 명시적으로 요구한다.
    model, base_url = required("FEEDBACK_LLM_MODEL"), required("FEEDBACK_LLM_BASE_URL")
    mcp_url, token = required("FEEDBACK_MCP_URL"), required("FEEDBACK_MCP_TOKEN")
    code = git_provenance(args.repo.resolve())
    async with AsyncOpenAI(
        base_url=base_url,
        api_key=os.getenv("FEEDBACK_LLM_API_KEY") or "self-hosted",
        timeout=45,
        max_retries=0,
    ) as client:
        agent = FeedbackAgent(client, model)

        async def execute(event, trace_id, provenance):
            async with LiveMCPGateway(token, mcp_url) as gateway:
                return await LiveFeedbackWorkflow(gateway, agent).run(
                    event,
                    trace_id=trace_id,
                    code_provenance=provenance,
                )

        worker = FeedbackWorker(
            execute,
            trace_dir=args.trace_dir,
            model=model,
            code_provenance=code,
            langsmith=args.langsmith,
        )
        if args.command == "worker":
            await consume_feedback(required("FEEDBACK_AMQP_URL"), worker)
            return 0

        # 로컬 1건 확인 모드. RabbitMQ에 발행/ack하지 않고 필요한 조치만 출력한다.
        async def publish(queue, payload, code):
            print(json.dumps({"would_publish": queue, "error_code": code}, ensure_ascii=False))

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
            LiveEvaluationEvent.model_validate_json(args.event.read_bytes())
            print(json.dumps({"status": "valid", "type": "LIVE_EVALUATE", "network_calls": 0}))
            return 0
        if args.command == "schemas":
            models = (
                LiveEvaluationEvent,
                LiveEvaluationContext,
                MessageBatch,
                GeneratedFeedback,
                SaveReceipt,
            )
            print(
                json.dumps(
                    {m.__name__: m.model_json_schema() for m in models},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "feedback_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
