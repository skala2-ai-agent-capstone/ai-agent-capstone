"""OpenAI 호환 요청을 vLLM에 손대지 않고 넘기고, 주고받은 내용을 llm_exchanges에 기록한다.

OPENAI_STAGES에 든 단계(X-Stage)는 vLLM 대신 OpenAI로 보낸다. 이때 모델 이름만 OPENAI_MODEL로
바꾸므로 호출한 쪽은 단계만 정하면 된다. 업무 공간 대화와 단계가 없는 호출은 언제나 vLLM이다.
팀원은 자기 프록시 키로 호출하고, 프록시는 vLLM(또는 OpenAI) 키로 바꿔 넘긴다. 키는 기록하지 않는다.
요청 헤더 X-Stage·X-Work-Id·X-User-Id가 있으면 함께 남긴다(vLLM에는 넘기지 않음).
llm_exchanges는 업무 DB의 외래 키·CHECK를 따르므로, 저장할 수 없는 헤더는
vLLM에 넘기기 전에 400으로 거절한다(답은 받았는데 기록만 사라지는 일을 막는다).
업무 공간 대화(workspace_chat)는 업무·사용자 헤더가 필수다.
"""

import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .settings import STAGES, Settings
from .store import PostgresRecorder, Recorder

log = logging.getLogger("llm_proxy")

MAX_TAG_LEN = 128
# 길이·인코딩은 프록시가 다시 정하므로 업스트림 값을 그대로 돌려주지 않는다
DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def error_body(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": code, "code": code}}


def find_member(request: Request, member_keys: dict[str, str]) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    for key, name in member_keys.items():
        if hmac.compare_digest(token.encode(), key.encode()):
            return name
    return None


def header_tag(request: Request, name: str) -> str | None:
    return request.headers.get(name, "").strip()[:MAX_TAG_LEN] or None


def parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def token_usage(body: Any) -> tuple[int | None, int | None]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, None
    # Chat Completions는 prompt/completion, Responses API는 input/output 이름을 쓴다
    return (
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
    )


def stream_usage(text: str) -> tuple[int | None, int | None]:
    """SSE 이벤트 중 마지막 usage.

    Responses API는 response.completed 이벤트의 response 안에 있다.
    """
    found: tuple[int | None, int | None] = (None, None)
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        event = parse_json(line[5:].strip().encode())
        if not isinstance(event, dict):
            continue
        for candidate in (event, event.get("response")):
            usage = token_usage(candidate)
            if usage != (None, None):
                found = usage
    return found


def response_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def save_quietly(store: Recorder, record: dict[str, Any]) -> None:
    try:
        await store.save(record)
    except Exception as exc:  # 기록 실패로 팀원의 LLM 호출이 실패하면 안 된다
        # 예외 문장에 요청 본문이 섞일 수 있어 종류만 남긴다
        log.warning("기록 저장 실패: %s", type(exc).__name__)


def create_app(
    settings: Settings,
    *,
    recorder: Recorder | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = None
        if recorder is None:
            owned = PostgresRecorder(settings.database_url)
            await owned.open()
        app.state.recorder = recorder or owned
        timeout = httpx.Timeout(settings.upstream_timeout, connect=10.0)
        async with httpx.AsyncClient(
            base_url=settings.upstream_url, timeout=timeout, transport=transport
        ) as client, httpx.AsyncClient(
            base_url=settings.openai_url, timeout=timeout, transport=transport
        ) as openai_client:
            app.state.client, app.state.openai_client = client, openai_client
            try:
                yield
            finally:
                if owned is not None:
                    await owned.close()

    app = FastAPI(title="llm-proxy", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def relay(path: str, request: Request) -> Response:
        member = find_member(request, settings.member_keys)
        if member is None:
            return JSONResponse(
                error_body("프록시 키가 없거나 올바르지 않습니다.", "invalid_api_key"),
                status_code=401,
            )
        store: Recorder = request.app.state.recorder
        stage = header_tag(request, "x-stage")
        external = stage in settings.openai_stages
        client: httpx.AsyncClient = (
            request.app.state.openai_client if external else request.app.state.client
        )
        api_key = settings.openai_api_key if external else settings.upstream_api_key

        url = f"/v1/{path}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if content_type := request.headers.get("content-type"):
            headers["Content-Type"] = content_type
        raw = await request.body()
        body = parse_json(raw) if request.method == "POST" else None
        if external and isinstance(body, dict) and "model" in body:
            # 로컬 모델 이름으로 불러도 OpenAI 모델로 보낸다. 기록에는 실제로 보낸 모델이 남는다
            body = {**body, "model": settings.openai_model}
            raw = json.dumps(body, ensure_ascii=False).encode()
        upstream = client.build_request(request.method, url, content=raw, headers=headers)

        record: dict[str, Any] = {
            "member": member,
            "endpoint": f"/v1/{path}",
            "stage": stage,
            "work_id": header_tag(request, "x-work-id"),
            "user_id": header_tag(request, "x-user-id"),
            "model": body.get("model") if isinstance(body, dict) else None,
            "stream": bool(isinstance(body, dict) and body.get("stream")),
            "request": body,
        }
        # 모델 목록 같은 조회는 기록하지 않는다
        background = request.method == "POST"
        if background and (problem := await trace_problem(store, record)):
            return JSONResponse(error_body(problem, "invalid_trace_headers"), status_code=400)

        started = time.perf_counter()
        try:
            resp = await client.send(upstream, stream=record["stream"])
        except httpx.TimeoutException:
            return failure(store, record, started, background, 504, "upstream_timeout",
                           "LLM 서버 응답 시간이 초과되었습니다.")
        except httpx.TransportError:
            return failure(store, record, started, background, 502, "upstream_unreachable",
                           "LLM 서버에 연결할 수 없습니다.")

        if record["stream"]:
            return StreamingResponse(
                relay_stream(store, resp, record, started),
                status_code=resp.status_code,
                headers=response_headers(resp),
            )

        parsed = parse_json(resp.content)
        input_tokens, output_tokens = token_usage(parsed)
        record.update(
            status_code=resp.status_code,
            error="upstream_error" if resp.status_code >= 400 else None,
            latency_ms=elapsed_ms(started),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response=parsed,
            response_text=resp.text if parsed is None and resp.content else None,
        )
        return Response(
            resp.content,
            status_code=resp.status_code,
            headers=response_headers(resp),
            background=BackgroundTask(save_quietly, store, record) if background else None,
        )

    return app


async def trace_problem(store: Recorder, record: dict[str, Any]) -> str | None:
    stage, work_id, user_id = record["stage"], record["work_id"], record["user_id"]
    if stage is not None and stage not in STAGES:
        return f"X-Stage는 {', '.join(sorted(STAGES))} 중 하나여야 합니다."
    if stage == "workspace_chat" and (work_id is None or user_id is None):
        return "업무 공간 대화(X-Stage: workspace_chat)에는 X-Work-Id와 X-User-Id가 필요합니다."
    if work_id is None and user_id is None:
        return None
    work_ok, user_ok = await store.known_refs(work_id, user_id)
    if not work_ok:
        return "X-Work-Id가 업무 DB에 없는 업무입니다."
    if not user_ok:
        return "X-User-Id가 업무 DB에 없는 사용자입니다."
    return None


def failure(
    store: Recorder, record: dict[str, Any], started: float, background: bool,
    status: int, code: str, message: str,
) -> JSONResponse:
    record.update(status_code=status, error=code, latency_ms=elapsed_ms(started))
    log.warning("업스트림 실패: %s %s", record["endpoint"], code)
    return JSONResponse(
        error_body(message, code),
        status_code=status,
        background=BackgroundTask(save_quietly, store, record) if background else None,
    )


async def relay_stream(
    store: Recorder, resp: httpx.Response, record: dict[str, Any], started: float
):
    parts: list[bytes] = []
    error = "upstream_error" if resp.status_code >= 400 else None
    try:
        async for chunk in resp.aiter_bytes():
            parts.append(chunk)
            yield chunk
    except httpx.HTTPError:
        error = "upstream_stream_broken"
    except BaseException:
        error = "client_disconnected"
        raise
    finally:
        await resp.aclose()
        text = b"".join(parts).decode("utf-8", "replace")
        input_tokens, output_tokens = stream_usage(text)
        record.update(
            status_code=resp.status_code,
            error=error,
            latency_ms=elapsed_ms(started),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response=None,
            response_text=text,
        )
        await save_quietly(store, record)


def app_from_env() -> FastAPI:
    """uvicorn --factory 진입점."""
    logging.basicConfig(level=logging.INFO)
    return create_app(Settings.from_env())
