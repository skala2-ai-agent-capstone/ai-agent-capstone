"""vLLM 대신 쓰는 모의 OpenAI 호환 서버 (GPU 없는 곳에서 llm-proxy까지 시험할 때). 표준 라이브러리만 쓴다.

- POST /v1/chat/completions: 일반 응답과 스트리밍(SSE, stream_options.include_usage) 둘 다
- 없는 모델 이름은 vLLM처럼 404, API 키가 다르면 401
- GET /v1/models, GET /health

    VLLM_API_KEY=… SERVED_MODEL_NAME=gemma4 python tests/mock_llm.py   # 포트 MOCK_LLM_PORT(기본 8000)
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("VLLM_API_KEY", "")
MODEL = os.environ.get("SERVED_MODEL_NAME", "gemma4")


def reply_for(messages: list[dict]) -> str:
    n = sum(1 for m in messages if m.get("role") == "user")
    return f"모의 응답 {n}: 요청하신 내용을 정리했습니다. 근거 자료의 조항 번호를 함께 확인해 주세요."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 요청마다 찍지 않는다
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if API_KEY and self.headers.get("Authorization") != f"Bearer {API_KEY}":
            self._json(401, {"error": {"message": "Unauthorized", "type": "AuthenticationError", "code": 401}})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/v1/models":
            if self._authorized():
                self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock-llm"}]})
            return
        self._json(404, {"error": {"message": "Not Found", "code": 404}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "Not Found", "code": 404}})
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("model") != MODEL:
            return self._json(404, {"error": {"message": f"The model `{body.get('model')}` does not exist.",
                                              "type": "NotFoundError", "code": 404}})
        text = reply_for(body.get("messages") or [])
        usage = {"prompt_tokens": 40, "completion_tokens": len(text) // 2, "total_tokens": 40 + len(text) // 2}
        base = {"id": f"chatcmpl-mock-{time.time_ns()}", "created": int(time.time()), "model": MODEL}
        if not body.get("stream"):
            return self._json(200, {**base, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = {**base, "object": "chat.completion.chunk"}
        events = [{**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
        events += [{**chunk, "choices": [{"index": 0, "delta": {"content": text[i:i + 12]}}]} for i in range(0, len(text), 12)]
        events.append({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if (body.get("stream_options") or {}).get("include_usage"):
            events.append({**chunk, "choices": [], "usage": usage})
        for e in events:
            self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_LLM_PORT", "8000"))
    print(f"mock-llm :{port} model={MODEL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
