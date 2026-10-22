"""로컬 JSONL + 선택적 LangSmith. 원문/예외 메시지/인증 객체는 전송하지 않는다."""

import inspect
import json
import os
import re
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from langsmith import Client, tracing_context
from langsmith.run_trees import RunTree
from pydantic import BaseModel

_SESSION = ContextVar("evaluation_trace_session", default=None)
_PARENT = ContextVar("evaluation_trace_parent", default=None)
_METADATA = {
    "request_id",
    "run_id",
    "snapshot_id",
    "snapshot_sha256",
    "purpose",
    "model",
    "prompt_version",
    "rubric_version",
    "dataset_version",
    "experiment_id",
    "git_commit",
}


def fingerprint(value) -> dict:
    """임의 객체의 repr를 쓰지 않는다. 알 수 없는 객체는 타입 이름만 사용한다."""

    def encode(obj):
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        return {"object_type": type(obj).__name__}

    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=encode).encode()
    except (ValueError, TypeError, RecursionError):
        return {"type": type(value).__name__}
    return {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}


def safe_metadata(metadata: dict) -> dict:
    # 이메일·자유 텍스트·임의 메타데이터를 제외한다. 인증키는 허용 목록에 없다.
    return {
        k: v
        for k, v in metadata.items()
        if k in _METADATA and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", v)
    }


class LangSmithSink:
    """자동 instrumentation을 쓰지 않고 정제된 RunTree만 명시적으로 전송한다."""

    def __init__(self, project: str, client=None):
        self.client = client or Client()
        self.project = project
        self.runs = {}

    def start(self, event):
        parent = self.runs.get(event["parent_id"])
        kwargs = {
            "inputs": event["inputs"],
            "start_time": datetime.fromisoformat(event["started_at"]),
            "extra": {"metadata": event["metadata"]},
        }
        if parent is None:
            run = RunTree(
                name=event["name"],
                id=UUID(event["span_id"]),
                run_type=event["run_type"],
                project_name=self.project,
                ls_client=self.client,
                trace_id=UUID(event["trace_id"]),
                **kwargs,
            )
        else:
            run = parent.create_child(
                event["name"], event["run_type"], run_id=UUID(event["span_id"]), **kwargs
            )
        self.runs[event["span_id"]] = run
        run.post()

    def finish(self, event):
        run = self.runs[event["span_id"]]
        run.end(
            outputs=event["outputs"],
            error=event["error_type"],
            end_time=datetime.fromisoformat(event["ended_at"]),
        )
        run.patch()

    def close(self):
        self.client.flush()


class Span:
    def __init__(self):
        self.outputs = {}

    def result(self, value):
        self.outputs = fingerprint(value)


class TraceSession:
    """로컬 기록은 필수. LangSmith 전송은 langsmith=True일 때만 실행한다."""

    def __init__(self, directory: Path, *, metadata=None, langsmith=False, project=None, sink=None):
        self.directory = Path(directory)
        self.trace_id = str(uuid4())
        self.path = self.directory / f"trace-{self.trace_id}.jsonl"
        self.metadata = safe_metadata(metadata or {})
        self.sink = sink
        self._enabled = langsmith
        self._project = project or os.getenv("LANGSMITH_PROJECT", "sales-ai-evaluation")
        self.telemetry_failed = False
        self._root_started = False

    def __enter__(self):
        if self._enabled and self.sink is None:
            if not os.getenv("LANGSMITH_API_KEY"):
                raise ValueError("LANGSMITH_API_KEY is required for --langsmith")
            self.sink = LangSmithSink(self._project)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(fd, "w", encoding="utf-8")
        # LangGraph 자동 추적은 전체 state를 포함할 수 있다. 이 범위에서는 항상 끈다.
        self._disabled_auto = tracing_context(enabled=False)
        self._disabled_auto.__enter__()
        self._session_token = _SESSION.set(self)
        self._parent_token = _PARENT.set(None)
        return self

    def __exit__(self, *exc):
        try:
            if self.sink:
                self._send("close")
        finally:
            _PARENT.reset(self._parent_token)
            _SESSION.reset(self._session_token)
            self._disabled_auto.__exit__(*exc)
            self._stream.close()

    def _send(self, method, *args):
        try:
            getattr(self.sink, method)(*args)
        except Exception:
            self.telemetry_failed = True
            warnings.warn(
                "Trace export failed; local execution continues.", RuntimeWarning, stacklevel=2
            )

    def _write(self, event):
        self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._stream.flush()

    @contextmanager
    def span(self, name: str, *, inputs=None, run_type="chain"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
            raise ValueError("trace name must be a static identifier")
        if run_type not in {"chain", "tool", "llm"}:
            raise ValueError("unsupported trace run type")
        span = Span()
        parent_id = _PARENT.get()
        if parent_id is None:
            if self._root_started:
                raise ValueError("use one root span per TraceSession")
            self._root_started = True
        event = {
            "event": "start",
            "trace_id": self.trace_id,
            "span_id": str(uuid4()) if parent_id else self.trace_id,
            "parent_id": parent_id,
            "name": name,
            "run_type": run_type,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "inputs": fingerprint(inputs),
            "metadata": self.metadata,
        }
        self._write(event)
        if self.sink:
            self._send("start", event)
        token = _PARENT.set(event["span_id"])
        started = perf_counter()
        error_type = None
        try:
            yield span
        except BaseException as exc:
            error_type = type(exc).__name__  # 메시지·traceback에는 원문이 있을 수 있다.
            raise
        finally:
            _PARENT.reset(token)
            end = {
                **event,
                "event": "end",
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "error_type": error_type,
                "outputs": span.outputs,
            }
            self._write(end)
            if self.sink:
                self._send("finish", end)


def trace_step(name: str, *, run_type="chain"):
    """기존 함수의 결과·예외를 그대로 보존한다. 세션 밖에서는 기록하지 않는다."""

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args, **kwargs):
                session = _SESSION.get()
                if session is None:
                    return await fn(*args, **kwargs)
                with session.span(
                    name, inputs={"args": args, "kwargs": kwargs}, run_type=run_type
                ) as span:
                    result = await fn(*args, **kwargs)
                    span.result(result)
                    return result

            return async_wrapper

        @wraps(fn)
        def wrapper(*args, **kwargs):
            session = _SESSION.get()
            if session is None:
                return fn(*args, **kwargs)
            with session.span(
                name, inputs={"args": args, "kwargs": kwargs}, run_type=run_type
            ) as span:
                result = fn(*args, **kwargs)
                span.result(result)
                return result

        return wrapper

    return decorate


@contextmanager
def trace_span(name, *, inputs=None, run_type="chain"):
    session = _SESSION.get()
    if session is None:
        yield Span()
    else:
        with session.span(name, inputs=inputs, run_type=run_type) as span:
            yield span


class TracedLLMClient:
    """Responses.parse 경계만 감싼다. 기준·분류·채점 프롬프트는 변경하지 않는다."""

    def __init__(self, client):
        self._client = client
        self.responses = _TracedResponses(client.responses)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _TracedResponses:
    def __init__(self, responses):
        self._responses = responses

    def __getattr__(self, name):
        return getattr(self._responses, name)

    async def parse(self, **kwargs):
        with trace_span("llm.responses.parse", inputs=kwargs, run_type="llm") as span:
            result = await self._responses.parse(**kwargs)
            span.result(result)
            usage = getattr(result, "usage", None)
            counts = {
                k: getattr(usage, k, None)
                for k in ("input_tokens", "output_tokens", "total_tokens")
            }
            span.outputs["usage_metadata"] = {
                k: v for k, v in counts.items() if type(v) is int and v >= 0
            }
            return result
