"""ChatLLM 健壮性测试：有限重试 / 不可重试错误 / 上下文超限标记 / SSE 流。

用本地假 HTTP 服务器按序回状态码，验证重试次数、退避尊重、错误分类。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from emberpy.errors import LLMError
from emberpy.llm.chat import ChatLLM

OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "你好 retry"}}],
    "usage": {"total_tokens": 7},
}
CTX_BODY = {"error": {"message": "This model's maximum context length is 128000 tokens"}}
SSE_ONE = {"choices": [{"delta": {"content": "流式"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}


class _Handler(BaseHTTPRequestHandler):
    """按预置 scenario 逐次回包；记录请求数。"""

    scenarios: list[dict[str, Any]] = []
    hits: list[str] = []

    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length) if length else b""
        _Handler.hits.append(self.command)
        if not _Handler.scenarios:
            self.send_response(500)
            self.end_headers()
            return
        spec = _Handler.scenarios.pop(0)
        status = spec.get("status", 200)
        body = spec.get("body")
        stream = spec.get("stream", False)
        if stream:
            raw = b"".join(
                b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
                for chunk in body
            ) + b"data: [DONE]\n\n"
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        raw = json.dumps(body if body is not None else {}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (spec.get("headers") or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def server() -> Any:
    _Handler.scenarios = []
    _Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()


def _client(server: Any, retry_delays: tuple[float, ...] = (0.0,)) -> ChatLLM:
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return ChatLLM(api_key="test-key", model="deepseek-chat", base_url=base, timeout=5.0, retry_delays=retry_delays)


class TestRetry:
    def test_retries_429_then_succeeds(self, server: Any) -> None:
        _Handler.scenarios = [{"status": 429, "body": {"error": "rate limited"}}, {"status": 200, "body": OK_BODY}]
        llm = _client(server)
        reply = llm.complete([{"role": "user", "content": "hi"}])
        assert len(_Handler.hits) == 2
        assert reply.content == "你好 retry"

    def test_retry_after_header_short_circuits_backoff(self, server: Any) -> None:
        # Retry-After: 0 -> 不用等退避就重试（验证读取该头不报错）
        _Handler.scenarios = [
            {"status": 429, "body": {"error": "busy"}, "headers": {"Retry-After": "0"}},
            {"status": 200, "body": OK_BODY},
        ]
        llm = _client(server, retry_delays=(5.0,))  # 若没读头会等 5s，测试会慢/超时
        reply = llm.complete([{"role": "user", "content": "hi"}])
        assert reply.content == "你好 retry"

    def test_5xx_retries_then_raises_status(self, server: Any) -> None:
        _Handler.scenarios = [
            {"status": 500, "body": {"error": "boom"}},
            {"status": 500, "body": {"error": "boom"}},
        ]
        llm = _client(server, retry_delays=(0.0,))  # 共 2 次尝试
        with pytest.raises(LLMError) as exc:
            llm.complete([{"role": "user", "content": "hi"}])
        assert exc.value.status == 500
        assert exc.value.context_exceeded is False
        assert len(_Handler.hits) == 2


class TestNoRetry:
    def test_422_does_not_retry(self, server: Any) -> None:
        _Handler.scenarios = [{"status": 422, "body": {"error": "bad params"}}]
        llm = _client(server)
        with pytest.raises(LLMError) as exc:
            llm.complete([{"role": "user", "content": "hi"}])
        assert exc.value.status == 422
        assert len(_Handler.hits) == 1

    def test_context_exceeded_marked_no_retry(self, server: Any) -> None:
        _Handler.scenarios = [{"status": 400, "body": CTX_BODY}]
        llm = _client(server)
        with pytest.raises(LLMError) as exc:
            llm.complete([{"role": "user", "content": "hi"}])
        assert exc.value.status == 400
        assert exc.value.context_exceeded is True
        assert len(_Handler.hits) == 1  # 上下文超限不是瞬时错误，不重试


class TestStream:
    def test_stream_returns_content(self, server: Any) -> None:
        _Handler.scenarios = [{"status": 200, "body": [SSE_ONE], "stream": True}]
        llm = _client(server)
        received: list[str] = []
        reply = llm.stream_complete(
            [{"role": "user", "content": "hi"}],
            on_delta=lambda text, _t: received.append(text),
        )
        assert reply.content == "流式"
        assert received[-1] == "流式"

    def test_stream_retries_429_then_200(self, server: Any) -> None:
        _Handler.scenarios = [
            {"status": 429, "body": {"error": "busy"}},
            {"status": 200, "body": [SSE_ONE], "stream": True},
        ]
        llm = _client(server)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}])
        assert reply.content == "流式"
        assert len(_Handler.hits) == 2
