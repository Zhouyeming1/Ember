"""worker 子 agent 桥接测试：桌面主 agent 通过 run_agent 派生子 agent。

整条事件链路走通：主 prompt -> 主模型要 run_agent -> 引擎内同步跑子 agent
（消费同一条 FakeLLM 脚本的下一个回复）-> 子 agent 最终文本作为 run_agent 的
工具结果（tool_execution_end.result）-> 主模型给最终答复。对 renderer 来讲就是
一条普通工具轨迹，无需新事件类型。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


def make_worker(workspace: Path, script: list[dict[str, Any]]) -> tuple[Worker, list[str]]:
    sink: list[str] = []
    worker = Worker(workspace=workspace, llm=FakeLLM(script), out=_Out(sink.append))
    return worker, sink


def run_prompt(worker: Worker, sink: list[str], message: str) -> None:
    rid = "p1"
    worker.handle_line(json.dumps({"type": "prompt", "id": rid, "message": message}))
    deadline = time.time() + 10
    while time.time() < deadline:
        if worker._run_thread is None or not worker._run_thread.is_alive():
            break
        time.sleep(0.02)
    deadline = time.time() + 10
    while time.time() < deadline:
        for line in sink:
            obj = json.loads(line)
            if obj.get("type") == "response" and obj.get("id") == rid:
                return
        time.sleep(0.02)
    raise AssertionError("prompt 没有返回响应")


def _lines(sink: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in sink]


def test_worker_main_agent_spawns_subagent_and_relays(workspace: Path) -> None:
    # 脚本顺序：主模型要 run_agent -> 子 agent 答复 -> 主模型最终答复（三条，同一 FakeLLM）
    script = [
        {
            "content": None,
            "calls": [tool_call("s1", "run_agent",
                                {"description": "统计符号", "prompt": "数一下 main.py 里有多少个函数", "agent_type": "explore"})],
        },
        {"content": "子答复：共 3 个函数", "calls": None},
        {"content": "主答复：已让子 agent 数完，结果是 3 个函数。", "calls": None},
    ]
    worker, sink = make_worker(workspace, script)
    run_prompt(worker, sink, "帮我数函数")

    lines = _lines(sink)
    # 主 agent 派生：出现 run_agent 工具轨迹
    starts = [o for o in lines if o.get("type") == "tool_execution_start"]
    assert any(o.get("toolName") == "run_agent" for o in starts)
    # 子 agent 最终文本成为 run_agent 的工具结果
    ends = [o for o in lines if o.get("type") == "tool_execution_end" and o.get("toolName") == "run_agent"]
    assert ends and "子答复：共 3 个函数" in ends[0]["result"]
    # 主 agent 最终答复
    assert any("主答复：已让子 agent 数完" in _assistant_text(o) for o in lines if o.get("type") == "message_end")
    # 子 agent 的会话是临时的，主会话事件里只有一轮（user + 带 run_agent 的 assistant + tool + assistant）
    assert worker.session is not None
    assert len(worker.session.events()) == 4


def _assistant_text(obj: dict[str, Any]) -> str:
    message = obj.get("message") or {}
    content = message.get("content") or []
    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(parts)
