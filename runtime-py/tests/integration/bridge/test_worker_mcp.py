"""worker MCP 桥接测试：EMBERPY_MCP_CONFIG 指向配置 JSON → 主 agent 能调 MCP 工具。

对 renderer 来讲就是一条普通工具轨迹（tool_execution_start/end toolName=
mcp__<server>__<tool>），无需新事件类型。无 EMBERPY_MCP_CONFIG 时 worker 完全
不建 MCP（不 spawn 任何进程）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from emberpy.mcp.config import MCP_CONFIG_ENV
from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call

FAKE_SERVER = str(Path(__file__).resolve().parents[2] / "fake_mcp_server.py")


def _write_config(tmp_path: Path) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FAKE_SERVER]}}}),
        encoding="utf-8",
    )
    return str(path)


def _make_worker(workspace: Path, script: list[dict[str, Any]]) -> tuple[Worker, list[str]]:
    sink: list[str] = []
    worker = Worker(workspace=workspace, llm=FakeLLM(script), out=_Out(sink.append))
    return worker, sink


def _run_prompt(worker: Worker, sink: list[str], message: str) -> None:
    rid = "p1"
    worker.handle_line(json.dumps({"type": "prompt", "id": rid, "message": message}))
    deadline = time.time() + 15
    while time.time() < deadline:
        for line in sink:
            obj = json.loads(line)
            if obj.get("type") == "response" and obj.get("id") == rid:
                return
        if worker._run_thread is not None and not worker._run_thread.is_alive():
            # 线程结束但 response 未出 -> 继续等主线程发 response
            time.sleep(0.05)
        else:
            time.sleep(0.02)
    raise AssertionError("prompt 没有返回响应")


def _lines(sink: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in sink]


def _assistant_text(obj: dict[str, Any]) -> str:
    message = obj.get("message") or {}
    content = message.get("content") or []
    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(parts)


def test_worker_without_config_has_no_mcp(workspace: Path) -> None:
    worker, _ = _make_worker(workspace, [{"content": "ok", "calls": None}])
    assert worker.mcp is None
    worker.close()


def test_worker_with_config_relays_mcp_tool_trace(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_CONFIG_ENV, _write_config(tmp_path))
    script = [
        {
            "content": None,
            "calls": [tool_call("c1", "mcp__fake__echo", {"text": "桥接喊话", "tags": ["m9"]})],
        },
        {"content": "已让服务器回显", "calls": None},
    ]
    worker, sink = _make_worker(workspace, script)
    try:
        assert worker.mcp is not None  # 配置在 -> manager 建立
        _run_prompt(worker, sink, "帮我喊话")

        lines = _lines(sink)
        starts = [o for o in lines if o.get("type") == "tool_execution_start"]
        assert any(o.get("toolName") == "mcp__fake__echo" for o in starts)
        ends = [o for o in lines if o.get("type") == "tool_execution_end" and o.get("toolName") == "mcp__fake__echo"]
        assert ends, "应有一条 mcp 工具结束事件"
        assert ends[0].get("isError") is False
        assert "桥接喊话" in str(ends[0].get("result", ""))
        assert any("已让服务器回显" in _assistant_text(o) for o in lines if o.get("type") == "message_end")
    finally:
        worker.close()  # 关掉 MCP 子进程
