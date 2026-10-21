import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import ValidationError

from .classifier import ClassificationError, classify_work
from .models import ClassificationReport, ClassificationResult, WorkContext
from .provider import OpenAIClassifier
from .structured import DecisionEnvelope

MAX_FILE_BYTES = 1_000_000


def load_context(path: Path) -> WorkContext:
    # 무제한 파일을 메모리에 읽거나 조용히 잘라내지 않습니다.
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ClassificationError("입력 파일은 1MB 이하여야 합니다.")
    return WorkContext.model_validate_json(raw)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="더미 JSON 기반 영업 업무 분류")
    sub = root.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="입력 JSON 검사만 수행(API 호출 없음)")
    validate.add_argument("files", nargs="+", type=Path)
    sub.add_parser("schemas", help="입력·출력 JSON Schema 출력(API 호출 없음)")
    classify = sub.add_parser("classify", help="OpenAI API로 업무 유형 분류")
    classify.add_argument("file", type=Path)
    classify.add_argument("--model", help="OPENAI_MODEL보다 우선")
    classify.add_argument("--output", type=Path, help="결과 저장 경로(기존 파일 덮어쓰기 금지)")
    return root


async def run_classification(args: argparse.Namespace) -> str:
    context = load_context(args.file)
    if args.output and args.output.exists():
        raise ClassificationError("출력 파일이 이미 존재합니다. 새 경로를 지정하세요.")
    # 현재 폴더의 .env만 읽고 이미 설정된 환경변수는 덮어쓰지 않습니다.
    load_dotenv(Path.cwd() / ".env", override=False)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = (args.model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
    if not api_key:
        raise ClassificationError(
            "OPENAI_API_KEY가 필요합니다. .env.example을 .env로 복사해 설정하세요. "
            "키 없이 확인하려면 validate 명령을 사용하세요."
        )
    if not model:
        raise ClassificationError("OPENAI_MODEL 또는 --model을 설정하세요.")
    async with AsyncOpenAI(api_key=api_key, timeout=60.0, max_retries=1) as client:
        report = await classify_work(context, OpenAIClassifier(client, model))
    return report.model_dump_json(indent=2)


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "validate":
            for path in args.files:
                context = load_context(path)
                print(
                    f"OK {path} "
                    f"(메시지 {len(context.messages)}, 산출물 {len(context.artifacts)})"
                )
            return 0
        if args.command == "schemas":
            print(json.dumps({
                "input": WorkContext.model_json_schema(),
                "llm_output": DecisionEnvelope.model_json_schema(),
                "classification": ClassificationResult.model_json_schema(),
                "report": ClassificationReport.model_json_schema(),
            }, ensure_ascii=False, indent=2))
            return 0
        output = asyncio.run(run_classification(args))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(output + "\n")
        print(output)
        return 0
    except ValidationError as exc:
        # 입력 본문을 포함하는 기본 ValidationError repr는 출력하지 않습니다.
        details = [
            {"location": ".".join(map(str, e["loc"])), "type": e["type"]}
            for e in exc.errors(include_input=False, include_url=False)
        ]
        print("Schema 검증 실패: " + json.dumps(details, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ClassificationError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("실행을 중단했습니다.", file=sys.stderr)
        return 130

