"""McpManager：命名/发现/路由/失败降级/断线自愈。

聚焦 manager 层（进程控制细节在 test_mcp_client.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

from emberpy.mcp.config import McpServerConfig
from emberpy.mcp.manager import (
    McpManager,
    _clean_description,
    _safe_schema,
    normalize_name,
    split_full_name,
)

FAKE_SERVER = str(Path(__file__).resolve().parents[2] / "fake_mcp_server.py")


def _cfg(name: str = "fake", *, server: str = FAKE_SERVER) -> McpServerConfig:
    return McpServerConfig(name=name, command=sys.executable, args=[server])


@pytest.fixture()
def manager(workspace: Path) -> Iterator[McpManager]:
    m = McpManager([_cfg()], cwd=str(workspace))
    m.ensure_ready()
    yield m
    m.close()


# -- 命名与 schema 处理 -------------------------------------------------


def test_normalize_and_roundtrip() -> None:
    assert normalize_name("my server.a") == "my_server_a"
    # tool 名本身含 __ 也能还原（第二段切一次，余下整体是 tool 名）
    assert split_full_name("mcp__server__tool__with__sep") == ("server", "tool__with__sep")
    assert split_full_name("read_file") is None


def test_description_fold_and_truncate() -> None:
    long_desc = "line1\nline2   spaced\n" + "x" * 3000
    cleaned = _clean_description(long_desc)
    assert "\n" not in cleaned
    assert len(cleaned) == 2048


def test_schema_fallback() -> None:
    assert _safe_schema({"type": "object", "properties": {"a": {}}})["type"] == "object"
    fixed = _safe_schema({"properties": {"a": {}}})  # 缺顶层 type -> object
    assert fixed["type"] == "object"
    fixed2 = _safe_schema("not a schema")  # 完全非法 -> 兜底
    assert fixed2["type"] == "object" and fixed2["properties"] == {}
    assert _safe_schema({"type": "string"})["type"] == "object"  # 非 object 顶 -> 包成 object


# -- 连接与发现 ---------------------------------------------------------


def test_ensure_ready_discovers_tools(manager: McpManager) -> None:
    names = [e.full_name for e in manager.tool_entries()]
    assert "mcp__fake__echo" in names
    assert "mcp__fake__fail" in names
    entry = next(e for e in manager.tool_entries() if e.tool_name == "echo")
    assert entry.server_name == "fake"
    assert entry.schema["type"] == "object"
    assert entry.schema["properties"]["text"]["type"] == "string"
    assert "来自 MCP 服务器" not in entry.description  # 原样描述（不带 manager 层后缀）


def test_ensure_ready_is_idempotent(manager: McpManager) -> None:
    first = manager.tool_entries()
    manager.ensure_ready()
    assert manager.tool_entries() == first


def test_broken_server_skipped_not_fatal(workspace: Path) -> None:
    m = McpManager([_cfg(name="good"), _cfg(name="bad", server="no-such-file.py")], cwd=str(workspace))
    m.ensure_ready()  # 不抛
    names = [e.full_name for e in m.tool_entries()]
    assert any(n.startswith("mcp__good__") for n in names)
    assert not any(n.startswith("mcp__bad__") for n in names)
    assert "bad" in m.failures()
    m.close()


# -- 调用路由 -----------------------------------------------------------


def test_invoke_echo(manager: McpManager) -> None:
    text, is_error = manager.invoke("mcp__fake__echo", {"text": "回显一下", "tags": ["t1"]})
    assert is_error is False
    assert "回显一下" in text and "[tags: t1]" in text


def test_invoke_error_result_flags(manager: McpManager) -> None:
    text, is_error = manager.invoke("mcp__fake__fail", {})
    assert is_error is True and "故意失败" in text


def test_invoke_structured_flattened(manager: McpManager) -> None:
    text, is_error = manager.invoke("mcp__fake__structured", {})
    assert is_error is False
    assert '"ok": true' in text and "1" in text


def test_invoke_unknown_server(manager: McpManager) -> None:
    text, is_error = manager.invoke("mcp__nope__tool", {})
    assert is_error is True and "找不到 MCP server" in text


def test_invoke_non_mcp_name(manager: McpManager) -> None:
    text, is_error = manager.invoke("read_file", {})
    assert is_error is True and "不是 MCP 工具" in text


def test_invoke_after_server_killed_selfheals(manager: McpManager) -> None:
    # 杀掉 fake server 的进程后调用：invoke 自动重连一次并成功返回
    session = manager._by_norm["fake"]
    assert session._proc is not None
    session._proc.terminate()
    session._proc.wait(timeout=3)
    text, is_error = manager.invoke("mcp__fake__echo", {"text": "死而复生"})
    assert is_error is False and "死而复生" in text
