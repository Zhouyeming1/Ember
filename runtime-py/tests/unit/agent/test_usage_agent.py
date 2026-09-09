"""Agent 会话级 usage/cost 累积：主循环每次模型答复 track、子 agent 并入父。

口径见 tests/unit/llm/test_usage.py。这里验证接线：FakeLLM 脚本项带 "usage"
（模拟 DeepSeek 真实响应的 usage）跑 agentic 循环后，agent.usage 把父+子的
token/cost 都累起来了，worker 才能填 stats 面板。
"""
from __future__ import annotations

import pytest

from emberpy.testing import FakeLLM, make_agent, tool_call

CHILD = {"prompt_tokens": 500, "completion_tokens": 30}
P1 = {"prompt_tokens": 900, "completion_tokens": 10, "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 200}
P2 = {"prompt_tokens": 200, "completion_tokens": 40}


def test_tracks_single_reply_usage(workspace) -> None:
    agent = make_agent(
        [{"content": "直接回答", "calls": None, "usage": P2}],
        workspace,
        mode="auto",
    )
    agent.run("任务")
    assert agent.usage.any_usage
    assert agent.usage.output == 40
    assert agent.usage.input == 200  # 无 cache 字段 -> 整个 prompt 都是 input
    assert agent.usage.cache_read == 0
    # 最近一次 prompt_tokens 供 context 圆环
    assert agent.usage.context_usage(64_000) == {"tokens": 200, "contextWindow": 64_000, "percent": 0.3}


def test_accumulates_across_tool_turns(workspace) -> None:
    agent = make_agent(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})], "usage": P1},
            {"content": "读完了", "calls": None, "usage": P2},
        ],
        workspace,
        mode="auto",
    )
    agent.run("读文件")
    # P1: hit700+miss200 == prompt900 -> input 0；P2: input 200
    assert agent.usage.output == 50
    assert agent.usage.input == 200
    assert agent.usage.cache_read == 700
    assert agent.usage.cache_write == 200
    assert agent.usage.tokens_dict()["total"] == 200 + 50 + 700 + 200
    assert agent.usage.cost > 0


def test_no_usage_replies_leave_tracker_empty(workspace) -> None:
    agent = make_agent([{"content": "嗯", "calls": None}], workspace, mode="auto")
    agent.run("任务")
    # FakeLLM 缺省 usage 是全 0 的 total_tokens:0 -> 不算"真实用量"
    assert not agent.usage.any_usage
    assert agent.usage.tokens_dict()["total"] == 0


def test_subagent_usage_merged_into_parent(workspace) -> None:
    child_llm = FakeLLM([{"content": "子完成", "calls": None, "usage": CHILD}])
    agent = make_agent(
        [
            {
                "content": None,
                "calls": [tool_call("s1", "run_agent", {"description": "调研", "prompt": "看下", "agent_type": "general"})],
                "usage": P1,
            },
            {"content": "父结束", "calls": None, "usage": P2},
        ],
        workspace,
        mode="auto",
        allow_subagents=True,
        subagent_llm=child_llm,
    )
    result = agent.run("派子 agent")
    assert result.final_content == "父结束"
    # 子 agent 的 30 output 并入父的统计
    assert agent.usage.output == 10 + 40 + 30
    # 子 agent 无 cache -> 全 input；父 P1 无新鲜 input
    assert agent.usage.input == 200 + 500
    assert child_llm.complete_calls, "子 agent 应真的跑过模型"
