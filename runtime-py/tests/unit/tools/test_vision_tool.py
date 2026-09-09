"""vision 工具单测：读图→fake 视觉 API→识别文本回模型；白名单/无 Key/无图护栏。

fake server 是本地 stdlib HTTP，返回 OpenAI 兼容 chat/completions；断言请求体确实
带了 image_url 的 base64 data URI（图片字节真被送往视觉端点）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from emberpy.testing import FakeLLM, make_agent, tool_call
from emberpy.tools import ToolEnv, build_vision_tool


class _Handler(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received = body
        content = body["messages"][0]["content"]
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]
        ok_auth = self.headers.get("authorization") == "Bearer sk-x"
        if not ok_auth or not blocks or not str(blocks[0]["image_url"]["url"]).startswith("data:image/png;base64,"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"bad request shape"}}')
            return
        payload = json.dumps({"choices": [{"message": {"content": "图里有：一个红色的圆圈"}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def vision_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/chat/completions"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vision_url: str) -> ToolEnv:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    cfg = tmp_path / "vision.json"
    cfg.write_text(
        json.dumps({"provider": "custom", "endpoint": vision_url, "model": "fake-vision", "apiKey": "sk-x"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMBERPY_VISION_CONFIG", str(cfg))
    return ToolEnv(workspace=workspace, gate=None, patches=None)


def _png(workspace: Path, name: str = "shot.png") -> Path:
    p = workspace / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not-a-real-png-body" * 8)
    return p


def test_registered_name_and_schema(env: ToolEnv) -> None:
    tool = build_vision_tool(env)
    assert tool.name == "vision"  # renderer 按字面量识别成"识图"轨迹
    schema = tool.schema()["function"]["parameters"]
    assert schema["required"] == ["paths"]


def test_calls_vision_and_returns_text(env: ToolEnv, vision_url: str) -> None:
    image = _png(env.workspace)
    tool = build_vision_tool(env)
    out = tool.fn(paths=[str(image)], prompt="图里有什么")
    assert out == "图里有：一个红色的圆圈"
    # 请求体确实把图片字节以 image_url 发给了视觉端点，且带上了配置的 API Key
    assert _Handler.received["model"] == "fake-vision"


def test_no_images_returns_guidance(env: ToolEnv) -> None:
    tool = build_vision_tool(env)
    out = tool.fn()
    assert out.startswith("错误")
    assert "没有图片" in out


def test_path_outside_roots_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vision_url: str) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    cfg = tmp_path / "vision.json"
    cfg.write_text(json.dumps({"provider": "custom", "endpoint": vision_url, "model": "m", "apiKey": "k"}))
    monkeypatch.setenv("EMBERPY_VISION_CONFIG", str(cfg))
    tool = build_vision_tool(ToolEnv(workspace=workspace, gate=None, patches=None))
    out = tool.fn(paths=[str(outside)])
    assert out.startswith("错误")
    assert "允许目录" in out


def test_missing_key_gives_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    image = _png(workspace)
    cfg = tmp_path / "vision.json"
    cfg.write_text(json.dumps({"provider": "custom", "endpoint": "http://127.0.0.1:9/x", "model": "m"}))  # 无 apiKey
    monkeypatch.setenv("EMBERPY_VISION_CONFIG", str(cfg))
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    tool = build_vision_tool(ToolEnv(workspace=workspace, gate=None, patches=None))
    out = tool.fn(paths=[str(image)])
    assert "未配置视觉模型 API Key" in out


def test_unreadable_file_returns_error(env: ToolEnv) -> None:
    missing = env.workspace / "ghost.png"
    tool = build_vision_tool(env)
    out = tool.fn(paths=[str(missing)])
    assert out.startswith("错误")


def test_agent_calls_vision_tool_end_to_end(env: ToolEnv) -> None:
    """agentic 循环里模型真的触发 vision 工具并把识别文本当结果用。"""
    image = _png(env.workspace, "ui.png")
    agent = make_agent(
        [
            {
                "content": None,
                "calls": [tool_call("v1", "vision", {"paths": [str(image)], "prompt": "描述这张截图"})],
            },
            {"content": "我看到图里有圆圈了", "calls": None},
        ],
        env.workspace,
        mode="auto",
    )
    result = agent.run("看一下这张截图")
    assert result.final_content == "我看到图里有圆圈了"
    # 工具结果（识别文本）确实回填进了会话，模型能"看到"图
    tool_result = [m for m in agent.session.messages() if m.get("role") == "tool"][-1]
    assert "一个红色的圆圈" in tool_result["content"]
