"""worker stats 接线：真实 prompt（假模型带 usage）跑完后，_get_stats 返回面板字段。

对应 Ember AgentSessionStats 契约：tokens{input/output/cacheRead/cacheWrite/total}、
cost、contextUsage{tokens/contextWindow/percent}。无真实用量的会话保持全 0 且不发
contextUsage（UI 走"首次回复后出现"的 fallback）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM

USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 33,
    "prompt_cache_hit_tokens": 900,
    "prompt_cache_miss_tokens": 300,
}


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
        time.sleep(0.02)
    raise AssertionError("prompt 没有返回响应")


def test_stats_reflect_real_usage(workspace: Path) -> None:
    script = [{"content": "统计完毕", "calls": None, "usage": USAGE}]
    worker, sink = _make_worker(workspace, script)
    _run_prompt(worker, sink, "跑一下")
    try:
        stats = worker._get_stats()
        # prompt=1200 全被 hit/miss 记账 -> input 0；DeepSeek 无"新鲜"输入
        assert stats["tokens"] == {
            "input": 0,
            "output": 33,
            "cacheRead": 900,
            "cacheWrite": 300,
            "total": 1233,
        }
        assert stats["cost"] == round(900 / 1e6 * 0.07 + 300 / 1e6 * 0.27 + 33 / 1e6 * 1.10, 4)
        assert stats["contextUsage"] == {"tokens": 1200, "contextWindow": 64000, "percent": 1.9}
    finally:
        worker.close()


def test_stats_empty_before_real_usage(workspace: Path) -> None:
    worker, sink = _make_worker(workspace, [{"content": "嗯", "calls": None}])
    _run_prompt(worker, sink, "打招呼")
    try:
        stats = worker._get_stats()
        assert stats["tokens"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
        assert stats["cost"] == 0
        assert "contextUsage" not in stats  # 无真实用量 -> UI 走 fallback
    finally:
        worker.close()
