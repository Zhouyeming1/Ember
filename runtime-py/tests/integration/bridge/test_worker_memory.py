"""worker 记忆链路（跨会话持久）离线测试。

场景：
① 会话 A（EMBER_HOME + EMBERPY_MEMORY_DIR 指向临时目录，模型脚本里 memory_save）
   跑一轮 -> 记忆 .md + MEMORY.md 索引落盘；
   会话 B（全新 Worker、同一记忆目录、不同工作区）启动 -> 能列到 A 存的记忆，
   且 B 装配出的 agent 的 system prompt 记忆区块包含该索引条目（"会话 A 记住 ->
   会话 B 生效"）；
② 未启用记忆（无 EMBER_HOME / EMBER_SESSIONS_DIR env）时 registry 不含
   memory_* 工具、system prompt 没有记忆区块（默认工具集不变）；
③ 启用时 registry 含 memory_save/read/list/dir，空索引区块给"还没有记忆"提示，
   save 后区块携带索引行。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
    sink: list[str] = []
    worker = Worker(workspace=workspace, llm=FakeLLM(script), out=_Out(sink.append), **kwargs)
    return worker, sink


def send(worker: Worker, method: str, rid: str, **params: Any) -> None:
    worker.handle_line(json.dumps({"type": method, "id": rid, **params}, ensure_ascii=False))


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str = "p1") -> None:
    send(worker, "prompt", rid, message=message)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if worker._run_thread is None or not worker._run_thread.is_alive():
            break
        time.sleep(0.02)
    for line in reversed(sink):
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == rid:
            assert obj.get("success") is True
            return
    raise AssertionError("没有 prompt 响应。")


# ---------------------------------------------------------------------------
# ① 会话 A 记住 -> 会话 B 生效
# ---------------------------------------------------------------------------


def test_memory_survives_across_workers(tmp_path: Path, monkeypatch) -> None:
    mem_dir = tmp_path / "shared-mem"
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMBERPY_MEMORY_DIR", str(mem_dir))

    # 会话 A：模型在这一轮调用 memory_save，之后正常收尾
    script_a = [
        {
            "content": "先把项目背景记下来",
            "calls": [
                tool_call(
                    "c1",
                    "memory_save",
                    {
                        "name": "project_goal",
                        "type": "project",
                        "content": "目标：用 Python 从零实现 agent 引擎，前端复用现有 UI。",
                        "description": "项目目标",
                    },
                )
            ],
        },
        {"content": "已记住项目背景。"},
    ]
    worker_a, sink_a = make_worker(tmp_path / "proj-a", script_a)
    assert worker_a.memory is not None  # 有 env = 启用
    run_prompt(worker_a, sink_a, "记住项目背景")

    # 记忆文件 + MEMORY.md 索引落盘
    assert (mem_dir / "project_goal.md").is_file()
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [project_goal](project_goal.md)" in index

    # 会话 B：全新 Worker、不同工作区、同一记忆目录 -> 能看见 A 存的记忆
    worker_b, _ = make_worker(tmp_path / "proj-b", [{"content": "ok"}])
    assert worker_b.memory is not None
    names = [m.name for m in worker_b.memory.list()]
    assert "project_goal" in names

    # 生效证据：B 装配出的 agent，system prompt 记忆区块带上了 A 存的索引条目
    agent_b = worker_b._ensure_agent()
    sys_text = agent_b._system_prompt()["content"]
    assert "跨会话记忆" in sys_text
    assert "[project_goal](project_goal.md)" in sys_text
    assert "项目目标" in sys_text


# ---------------------------------------------------------------------------
# ② 未启用记忆：工具集与 system prompt 回到默认（无 memory_*）
# ---------------------------------------------------------------------------


def test_memory_off_by_default(monkeypatch) -> None:
    # 清掉可能让 persist=True 的 env（本机/CI 未知状态）
    for key in ("EMBER_HOME", "EMBER_SESSIONS_DIR", "EMBERPY_MEMORY_DIR"):
        monkeypatch.delenv(key, raising=False)

    worker, _ = make_worker(Path(".").resolve(), [{"content": "hi"}])
    assert worker.memory is None
    agent = worker._ensure_agent()
    assert "memory_save" not in agent.registry.names()
    assert "memory_read" not in agent.registry.names()
    sys_text = agent._system_prompt()["content"]
    assert "跨会话记忆" not in sys_text
    assert "还没有任何记忆" not in sys_text


# ---------------------------------------------------------------------------
# ③ 启用时：工具就位、空索引提示、save 后区块带索引行
# ---------------------------------------------------------------------------


def test_memory_on_registers_tools_and_index(tmp_path: Path, monkeypatch) -> None:
    mem_dir = tmp_path / "mem"
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMBERPY_MEMORY_DIR", str(mem_dir))

    worker, _ = make_worker(tmp_path / "proj", [{"content": "ok"}])
    agent = worker._ensure_agent()
    names = agent.registry.names()
    for expected in ("memory_save", "memory_read", "memory_list", "memory_dir"):
        assert expected in names

    empty_sys = agent._system_prompt()["content"]
    assert "跨会话记忆" in empty_sys
    assert "还没有任何记忆" in empty_sys

    # 直接经工具写入一条记忆，再取 system prompt：索引行出现、空提示消失
    save_tool = agent.registry.get("memory_save")
    out = save_tool.fn(
        name="feedback_terse",
        type="feedback",
        content="用户希望先给结论再展开，别废话。",
        description="回复要简洁直接",
    )
    assert "已新存记忆" in out
    full_sys = agent._system_prompt()["content"]
    assert "还没有任何记忆" not in full_sys
    assert "[feedback_terse](feedback_terse.md)" in full_sys
