"""기존 분류 CLI를 변경하지 않고 전체 승인·평가 흐름을 실행한다."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.types import Command
from openai import AsyncOpenAI
from pydantic import ValidationError

from .classifier import ClassificationError
from .evaluation_agent import render_evaluation_report
from .evaluation_errors import error_messages
from .evaluation_handoff import HandoffEvaluator
from .evaluation_models import AgentEvaluationOutput
from .evaluation_workflow import build_evaluation_workflow
from .mcp_cli import connection_settings, human_decision
from .mcp_client import MCPError, MCPGateway
from .provider import OpenAIClassifier
from .rubric_builder import OpenAIRubricBuilder
from .runtime_settings import setting


def parser():
    root = argparse.ArgumentParser(description="업무 분류·기준 승인·평가·코칭 실행")
    root.add_argument("--evaluation-id", help="개발용 내부 API로 실행 토큰 발급")
    root.add_argument("--work-id", help="발급받은 MCP_RUN_TOKEN 사용 시 필수")
    root.add_argument("--output", type=Path, help="전체 결과 JSON 파일(덮어쓰기 금지)")
    root.add_argument("--model", help="OPENAI_MODEL보다 우선")
    root.add_argument("--json", action="store_true", help="화면 결과를 JSON으로 출력")
    return root


async def run(args):
    # 이 평가 실행에만 프로젝트 설정을 우선 적용한다. 기존 분류 CLI는 변경하지 않는다.
    load_dotenv(Path.cwd() / ".env", override=True)
    if not os.getenv("MCP_RUN_TOKEN") and setting("RUN_TOKEN"):
        os.environ["MCP_RUN_TOKEN"] = setting("RUN_TOKEN")
    if args.output and args.output.exists():
        raise ValueError("출력 파일이 이미 존재합니다. 새 경로를 지정하세요.")
    key = setting("OPENAI_API_KEY").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY가 필요합니다.")
    token, work_id, business, rag = await connection_settings(args)
    model = args.model or setting("OPENAI_MODEL", "gpt-4o-mini")
    async with MCPGateway(token, business_url=business, rag_url=rag) as gateway:
        async with AsyncOpenAI(api_key=key, timeout=60, max_retries=1) as llm:
            workflow = build_evaluation_workflow(
                gateway,
                OpenAIClassifier(llm, model),
                OpenAIRubricBuilder(llm, model),
                evaluator=HandoffEvaluator(llm, gateway, model=model),
            )
            config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 100}
            result = await workflow.ainvoke({"work_id": work_id}, config)
            while result.get("__interrupt__"):
                decision = human_decision(result["__interrupt__"][0].value)
                result = await workflow.ainvoke(Command(resume=decision), config)
            return result


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(output + "\n")
            print(f"{result['status']}: {args.output}")
        elif result.get("status") == "completed" and not args.json:
            print(render_evaluation_report(AgentEvaluationOutput.model_validate(result["report"])))
        else:
            print(output)
        return 0
    except ExceptionGroup as exc:
        for message in error_messages(exc):
            print(f"오류: {message}", file=sys.stderr)
        return 2
    except ValidationError:
        print("오류: 입력/응답 스키마 불일치. 본문은 로그에 출력하지 않습니다.", file=sys.stderr)
        return 2
    except (MCPError, ClassificationError, ValueError, OSError) as exc:
        for message in error_messages(exc):
            print(f"오류: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("실행을 중단했습니다. 메모리의 승인 대기는 종료됩니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
