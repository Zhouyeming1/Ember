"""MCP stdio client：握手 / 工具发现 / 调用 / 错误与进程退出语义。

全程 spawn 本机 python 跑 tests/fake_mcp_server.py（离线，不碰网络）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

from emberpy.mcp.client import McpConnectionError, McpError, StdioMcpSession
from emberpy.mcp.config import McpServerConfig

FAKE_SERVER = str(Path(__file__).resolve().parents[2] / "fake_mcp_server.py")


@pytest.fixture()
def session() -> Iterator[StdioMcpSession]:
    cfg = McpServerConfig(name="fake", command=sys.executable, args=[FAKE_SERVER])
    s = StdioMcpSession(cfg, cwd=".")
    try:
        s.connect()
        yield s
    finally:
        s.close()


def test_handshake_and_tool_discovery(session: StdioMcpSession) -> None:
    assert session.is_alive()
    assert session._server_info.get("name") == "fake-mcp-server"
    tools = session.list_tools()
    assert [t["name"] for t in tools] == ["echo", "get_info", "fail", "structured"]
    # 缓存：第二次不重拉
    assert session.list_tools() == tools


def test_call_passes_arguments_verbatim(session: StdioMcpSession) -> None:
    result = session.call_tool("echo", {"text": "你好MCP", "repeat": 2, "tags": ["a", "b"]})
    blocks = result["content"]
    assert blocks[0]["type"] == "text"
    text = blocks[0]["text"]
    assert "你好MCP" in text and "[tags: a, b]" in text
    # repeat=2 → 出现两次（伪影只统计子串数量不精确，这里数换行分隔即可）
    assert text.count("你好MCP") == 2


def test_server_reported_error_is_not_raise(session: StdioMcpSession) -> None:
    result = session.call_tool("fail", {})
    assert result.get("isError") is True
    assert "故意失败" in result["content"][0]["text"]


def test_structured_content_without_text_block(session: StdioMcpSession) -> None:
    result = session.call_tool("structured", {})
    assert "content" not in result and "structuredContent" in result


def test_spawn_missing_command_raises() -> None:
    cfg = McpServerConfig(name="x", command="no-such-command-xyz-12345", args=[])
    s = StdioMcpSession(cfg, cwd=".")
    with pytest.raises(McpConnectionError):
        s.connect()
    assert not s.is_alive()


def test_server_that_exits_during_handshake_raises() -> None:
    # python -c pass：spawn 成功但握手时读到 EOF
    cfg = McpServerConfig(name="x", command=sys.executable, args=["-c", "pass"])
    s = StdioMcpSession(cfg, cwd=".")
    with pytest.raises(McpConnectionError):
        s.connect()
    assert not s.is_alive()


def test_polluted_stdout_is_reported(tmp_path: Path) -> None:
    # 一个把非 JSON 打到 stdout 的坏 server：协议通道被污染 -> McpError 而非静默
    bad = tmp_path / "bad_server.py"
    bad.write_text("import sys\nsys.stdout.write('hello from a broken server\\n')\nsys.stdout.flush()\n", encoding="utf-8")
    cfg = McpServerConfig(name="x", command=sys.executable, args=[str(bad)])
    s = StdioMcpSession(cfg, cwd=".")
    with pytest.raises(McpError):
        s.connect()


def test_reconnect_after_process_killed(session: StdioMcpSession) -> None:
    assert session._proc is not None
    session._proc.terminate()
    session._proc.wait(timeout=3)
    assert not session.is_alive()
    session.connect()  # 重新 spawn + 握手
    assert session.is_alive()
    tools = session.list_tools()
    assert any(t["name"] == "echo" for t in tools)


def test_close_is_idempotent(session: StdioMcpSession) -> None:
    session.close()
    session.close()
