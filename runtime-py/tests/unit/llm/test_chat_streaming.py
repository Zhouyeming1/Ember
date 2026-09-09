"""ChatLLM.stream_complete 真流式（SSE）离线测试：本机起 stdlib HTTP 服务。

不真连 DeepSeek；用 127.0.0.1 上的本地服务发 OpenAI 风格 SSE 分块，验证：
- 请求带 stream:True（reasoner 仍不带 temperature、聊天模型带）；
- reasoning_content / content 分开累计，on_delta 每次都拿到"累计全文/累计推理"；
- tool_calls 的 arguments 分片按 index 拼接成完整 JSON；
- [DONE] / finish_reason 正确收尾，返回的 AssistantReply 与整包同形状。
"""
from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

from emberpy.llm.chat import ChatLLM


class _TestServer(ThreadingHTTPServer):
    deltas: list[dict[str, Any]] = []
    request_bodies: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    server: _TestServer

    def log_message(self, *args: Any) -> None:  # 安静
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            self.server.request_bodies.append(json.loads(body))
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in self.server.deltas:
            line = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@contextlib.contextmanager
def sse_server(deltas: list[dict[str, Any]]) -> Iterator[tuple[str, _TestServer]]:
    """起本地 SSE 服务，yield (base_url, server)。server.request_bodies 收请求体。"""
    server = _TestServer(("127.0.0.1", 0), _Handler)
    server.deltas = deltas
    server.request_bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.shutdown()
        server.server_close()


def _delta(**fields: Any) -> dict[str, Any]:
    return {"choices": [{"delta": fields, "index": 0}]}


def _finish(reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"delta": {}, "index": 0, "finish_reason": reason}]}


# ---------------------------------------------------------------------------
# content / reasoning 流式累计
# ---------------------------------------------------------------------------


def test_reasoner_streams_thinking_then_content() -> None:
    deltas = [
        _delta(reasoning_content="先想"),
        _delta(reasoning_content="一下"),  # 服务端按增量发；实现累加成"先想一下"
        _delta(content="结果"),
        _delta(content="，讲完了"),
        _finish(),
    ]
    with sse_server(deltas) as (base_url, server):
        llm = ChatLLM(api_key="k", model="deepseek-reasoner", base_url=base_url)
        seen: list[tuple[str, str | None]] = []
        reply = llm.stream_complete(
            [{"role": "user", "content": "hi"}],
            on_delta=lambda text, thinking: seen.append((text, thinking)),
        )

    assert server.request_bodies[0]["stream"] is True
    assert "temperature" not in server.request_bodies[0]  # reasoner 不带采样参数
    # on_delta 每次给"累计全文 / 累计推理"
    assert seen[0] == ("", "先想")
    assert seen[1] == ("", "先想一下")
    assert seen[2] == ("结果", "先想一下")
    assert seen[-1] == ("结果，讲完了", "先想一下")
    assert reply.content == "结果，讲完了"
    assert reply.thinking == "先想一下"
    assert reply.raw_message["reasoning_content"] == "先想一下"
    assert reply.tool_calls == []


def test_chat_model_streams_with_temperature() -> None:
    deltas = [_delta(content="你好"), _delta(content="世界"), _finish()]
    with sse_server(deltas) as (base_url, server):
        llm = ChatLLM(api_key="k", model="deepseek-chat", base_url=base_url, temperature=0.3)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}])

    body = server.request_bodies[0]
    assert body["stream"] is True
    assert body["temperature"] == 0.3
    assert reply.content == "你好世界"
    assert reply.thinking is None


# ---------------------------------------------------------------------------
# tool_calls 分片拼接
# ---------------------------------------------------------------------------


def test_tool_call_arguments_accumulate_across_deltas() -> None:
    deltas = [
        _delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                            "function": {"name": "read_file", "arguments": ""}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"path":'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '"a.txt"}'}}]),
        _finish(reason="tool_calls"),
    ]
    with sse_server(deltas) as (base_url, _server):
        llm = ChatLLM(api_key="k", model="deepseek-chat", base_url=base_url)
        reply = llm.stream_complete([{"role": "user", "content": "read a.txt"}])

    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}  # 分片 JSON 拼接后完整可解析


def test_two_parallel_tool_calls_keep_index_separate() -> None:
    deltas = [
        _delta(tool_calls=[
            {"index": 0, "id": "c0", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"index": 1, "id": "c1", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]),
        _finish(reason="tool_calls"),
    ]
    with sse_server(deltas) as (base_url, _server):
        llm = ChatLLM(api_key="k", model="deepseek-chat", base_url=base_url)
        reply = llm.stream_complete([{"role": "user", "content": "go"}])

    assert [c.name for c in reply.tool_calls] == ["a", "b"]
    assert [c.arguments for c in reply.tool_calls] == [{}, {}]


def test_stream_empty_content_still_returns_reply() -> None:
    with sse_server([_finish()]) as (base_url, _server):
        llm = ChatLLM(api_key="k", model="deepseek-chat", base_url=base_url)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}])
    assert reply.content is None
    assert reply.tool_calls == []


def test_stream_captures_usage_only_trailing_chunk() -> None:
    # DeepSeek 流式在结束前常发一条 choices 为空的 usage 块
    deltas = [
        _delta(content="好"),
        {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
    ]
    with sse_server(deltas) as (base_url, _server):
        llm = ChatLLM(api_key="k", model="deepseek-chat", base_url=base_url)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}])
    assert reply.content == "好"
    assert reply.usage is not None
    assert reply.usage["total_tokens"] == 12

