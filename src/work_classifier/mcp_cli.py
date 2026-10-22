"""python -m work_classifier.mcp_cli fetch|run --evaluation-id ..."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.types import Command
from mcp.shared._httpx_utils import create_mcp_http_client
from openai import AsyncOpenAI
from pydantic import ValidationError

from .classification.agent import ClassificationError
from .classification.provider import OpenAIClassifier
from .integrations.mcp_client import MCPError, MCPGateway
from .integrations.mcp_data import classification_context, fetch_business
from .orchestration.graph import build_agent_graph
from .rubric.builder import OpenAIRubricBuilder


def parser():
    root = argparse.ArgumentParser(description="MCP 조회·분류·평가기준 승인·평가 입력 인계")
    root.add_argument("command", choices=["fetch", "run"])
    root.add_argument("--evaluation-id", help="개발용 내부 API로 실행 토큰 발급")
    root.add_argument("--work-id", help="발급받은 MCP_RUN_TOKEN 사용 시 필수")
    root.add_argument("--output", type=Path, help="JSON 출력 파일(덮어쓰기 금지)")
    root.add_argument("--model", help="OPENAI_MODEL보다 우선")
    return root


async def connection_settings(args):
    business_url = os.getenv("BUSINESS_MCP_URL", "http://localhost:8001/mcp").rstrip("/")
    rag_url = os.getenv("RAG_MCP_URL", "http://localhost:8002/mcp").rstrip("/")
    if not business_url.endswith("/mcp") or not rag_url.endswith("/mcp"):
        raise ValueError("BUSINESS_MCP_URL/RAG_MCP_URL은 /mcp까지 포함해야 합니다.")
    if args.evaluation_id:
        if args.work_id:
            raise ValueError("--evaluation-id와 --work-id는 함께 사용하지 않습니다.")
        key = os.getenv("INTERNAL_API_KEY", "").strip()
        if not key:
            raise ValueError("개발용 토큰 발급에는 INTERNAL_API_KEY가 필요합니다.")
        # 백엔드가 없는 개발 환경만 지원한다. 이 API는 LLM 도구로 공개하지 않는다.
        async with create_mcp_http_client(headers={"X-Internal-Key": key}) as client:
            try:
                response = await client.post(business_url[:-4] + "/internal/runs", json={
                    "evaluation_id": args.evaluation_id,
                })
                response.raise_for_status()
                result = response.json()
                if result.get("ok") is not True:
                    raise ValueError("발급 실패")
                token, work_id = result["data"]["run_token"], result["data"]["work_id"]
            except Exception:
                raise MCPError("RUN_ISSUE_FAILED", "internal/runs") from None
    else:
        token = os.getenv("MCP_RUN_TOKEN", "").strip()
        work_id = args.work_id
        if not token or not work_id:
            raise ValueError("MCP_RUN_TOKEN과 --work-id 또는 --evaluation-id가 필요합니다.")
    return token, work_id, business_url, rag_url


def human_decision(pending: dict) -> dict:
    print("\n[담당자 검토: 분류 결과와 평가기준 전체]", file=sys.stderr)
    warnings = pending.get("warnings", pending.get("criteria", {}).get("warnings", []))
    if warnings:
        print(f"[검토 경고 {len(warnings)}개: 아래 문맥을 확인한 뒤 결정하세요]", file=sys.stderr)
        for warning in warnings:
            print(f"- {warning['criterion_id']} / {warning['field']}: {warning['message']}",
                  file=sys.stderr)
            print(f"  해당 문장: {warning['excerpt']}", file=sys.stderr)
    print(json.dumps(pending, ensure_ascii=False, indent=2), file=sys.stderr)
    while True:
        print("승인 / 수정 / 취소: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except EOFError:
            answer = "취소"
        if answer == "취소":
            return {"action": "cancel"}
        if answer not in {"승인", "수정"}:
            continue
        decision = {k: pending[k] for k in (
            "criteria_sha256", "business_sha256", "revision",
        )}
        decision["action"] = "approve" if answer == "승인" else "revise"
        if answer == "수정":
            print("수정 요청: ", end="", file=sys.stderr, flush=True)
            try:
                decision["feedback"] = input().strip()
            except EOFError:
                return {"action": "cancel"}
            if not decision["feedback"]:
                continue
        return decision


async def run(args):
    load_dotenv(Path.cwd() / ".env", override=False)
    if args.output and args.output.exists():
        raise ValueError("출력 파일이 이미 존재합니다. 새 경로를 지정하세요.")
    if args.command == "run" and not os.getenv("OPENAI_API_KEY", "").strip():
        raise ValueError("run에는 OPENAI_API_KEY가 필요합니다. fetch는 LLM을 호출하지 않습니다.")
    token, work_id, business, rag = await connection_settings(args)
    async with MCPGateway(token, business_url=business, rag_url=rag) as gateway:
        if args.command == "fetch":
            packet = await fetch_business(gateway, work_id)
            context, mapping = classification_context(packet)
            return {"status": "fetched", "business": packet.model_dump(mode="json"),
                    "classification_input": context.model_dump(mode="json"), "source_map": mapping}
        model = args.model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        async with AsyncOpenAI(timeout=60, max_retries=1) as llm:
            graph = build_agent_graph(
                gateway, OpenAIClassifier(llm, model), OpenAIRubricBuilder(llm, model),
            )
            config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 100}
            result = await graph.ainvoke({"work_id": work_id}, config)
            while result.get("__interrupt__"):
                decision = human_decision(result["__interrupt__"][0].value)
                result = await graph.ainvoke(Command(resume=decision), config)
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
        else:
            print(output)
        return 0
    except ValidationError:
        print("오류: 입력/응답 스키마 불일치. 본문은 로그에 출력하지 않습니다.", file=sys.stderr)
        return 2
    except (MCPError, ClassificationError, ValueError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("실행을 중단했습니다. 메모리의 승인 대기는 종료됩니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
