"""승인된 입력으로 평가를 실행하고 결과를 화면에 출력한다. DB 저장 없음."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from openai import AsyncOpenAI
from pydantic import ValidationError

from .evaluation_agent import OpenAIEvaluator, render_evaluation_report
from .evaluation_validation import EvaluationError
from .evidence_models import EvaluationRun
from .mcp_adapter import MCPToolError
from .workflow_models import EvaluationInput


async def run(args) -> None:
    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    token = os.environ.get("RUN_TOKEN")
    if not api_key or not token:
        raise EvaluationError("OPENAI_API_KEY와 RUN_TOKEN 환경 변수가 필요합니다.")
    request = EvaluationInput.model_validate_json(args.input.read_text(encoding="utf-8"))
    raw = json.loads(args.run_context.read_text(encoding="utf-8"))
    raw = raw.get("data", raw)
    context = EvaluationRun.model_validate(
        {
            key: raw[key]
            for key in (
                "run_id",
                "work_id",
                "snapshot",
            )
        }
    )
    if request.run_id != context.run_id or request.work_record.work_id != context.work_id:
        raise EvaluationError("승인 입력과 실행 정보 파일이 다른 업무·실행입니다.")
    async with AsyncExitStack() as stack:
        clients = {}
        for name, url in (("business", args.business_url), ("rag", args.rag_url)):
            if not url.startswith(("http://", "https://")):
                raise EvaluationError("MCP 주소는 http:// 또는 https://로 시작해야 합니다.")
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
            clients[name] = await stack.enter_async_context(
                Client(
                    streamable_http_client(url, http_client=http),
                )
            )
        llm = await stack.enter_async_context(
            AsyncOpenAI(
                api_key=api_key,
                timeout=60.0,
                max_retries=1,
            )
        )

        async def business_call(name, arguments):
            return await clients["business"].call_tool(name, arguments)

        async def rag_call(name, arguments):
            return await clients["rag"].call_tool(name, arguments)

        agent = OpenAIEvaluator(
            llm,
            context=context,
            business_call=business_call,
            rag_call=rag_call,
        )
        result = await agent.evaluate(request)
        print(result.model_dump_json(indent=2) if args.json else render_evaluation_report(result))


def main() -> int:
    parser = argparse.ArgumentParser(description="승인된 업무의 AI 활용 평가 결과를 출력합니다.")
    parser.add_argument("--input", type=Path, required=True, help="승인된 EvaluationInput JSON")
    parser.add_argument("--run-context", type=Path, required=True, help="백엔드 실행 정보 JSON")
    parser.add_argument("--business-url", default="http://localhost:8001/mcp")
    parser.add_argument("--rag-url", default="http://localhost:8002/mcp")
    parser.add_argument("--json", action="store_true", help="화면에 JSON 결과 출력")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except ValidationError:
        print("입력·MCP 응답·모델 결과의 형식 또는 승인 정보를 확인하세요.", file=sys.stderr)
        return 2
    except MCPToolError as exc:
        print(f"MCP 조회 실패: {exc.code} (재시도 가능: {exc.retryable})", file=sys.stderr)
        return 2
    except EvaluationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, KeyError, ValueError, TypeError):
        print("파일·응답 구조 또는 원문·버전 검증에 실패했습니다.", file=sys.stderr)
        return 2
    except Exception:
        # 전송 오류에 인증 헤더·원문이 포함될 수 있으므로 그대로 출력하지 않는다.
        print(
            "서버 연결 또는 평가 실행에 실패했습니다. 서버 상태와 설정을 확인하세요.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
