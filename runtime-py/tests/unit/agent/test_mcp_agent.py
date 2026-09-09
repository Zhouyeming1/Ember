"""MCP 工具进 agent：工具暴露 / 权限三态 / 子 agent 不带外部工具。

一个运行中的 agent（auto/ask/plan/full 权限模式）真正调用到 fake MCP server，
验证端到端 + 授权边界。run_agent 子 agent 的工具列表绝不含 MCP 外部工具
（它的 child_env 不挂 manager，default_registry 因此不生成 mcp 工具）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from emberpy.agent import Agent
from emberpy.mcp.config import McpServerConfig
from emberpy.mcp.manager import McpManager
from emberpy.permission import PermissionMode
from emberpy.session import Session
from emberpy.testing import FakeLLM, tool_call
from emberpy.tools import ToolEnv, default_registry

FAKE_SERVER = str(Path(__file__).resolve().parents[2] / "fake_mcp_server.py")


def _mcp_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return tool_call("c1", name, args)


@pytest.fixture()
def manager(workspace: Path) -> Iterator[McpManager]:
    cfg = McpServerConfig(name="fake", command=sys.executable, args=[FAKE_SERVER])
    m = McpManager([cfg], cwd=str(workspace))
    yield m
    m.close()


def _make_agent(workspace, manager, mode: PermissionMode, llm, confirm=None, *, allow_subagents=False, subagent_llm=None) -> Agent:
    env = ToolEnv(workspace=workspace, gate=None, patches=None, confirm=confirm, mcp=manager)
    return Agent(
        llm=llm,
        workspace=workspace,
        mode=mode,
        env=env,
        session=Session(cwd=workspace),
        allow_subagents=allow_subagents,
        subagent_llm=subagent_llm,
    )


def _tool_events(agent: Agent) -> list[str]:
    return [
        str((e.get("message") or {}).get("content", ""))
        for e in agent.session.events()
        if e.get("type") == "tool"
    ]


# ---------------------------------------------------------------------------
# 工具暴露 + auto 模式真实调用
# ---------------------------------------------------------------------------


def test_mcp_tool_exposed_and_callable_in_auto(workspace: Path, manager: McpManager) -> None:
    llm = FakeLLM(
        [
            {"content": None, "calls": [_mcp_call("mcp__fake__echo", {"text": "引擎喊话", "tags": ["m9"]})]},
            {"content": "回显完毕", "calls": None},
        ]
    )
    agent = _make_agent(workspace, manager, PermissionMode.AUTO, llm)
    assert "mcp__fake__echo" in agent.registry.names()
    props = agent.registry.get("mcp__fake__echo").schema()["function"]["parameters"]["properties"]
    assert props["text"]["type"] == "string"  # 远端 schema 原样透传给模型

    result = agent.run("去 echo 一下")
    assert result.final_content == "回显完毕"
    # tool 事件里出现了 server 真实回显（换行分隔 repeat 1 次）
    assert any("引擎喊话" in t for t in _tool_events(agent))


# ---------------------------------------------------------------------------
# 权限三态：ask 无 confirm 拒 / ask 批准跑 / plan 拒 / full 跑
# ---------------------------------------------------------------------------


def test_ask_without_confirm_denies(workspace: Path, manager: McpManager) -> None:
    llm = FakeLLM(
        [
            {"content": None, "calls": [_mcp_call("mcp__fake__echo", {"text": "不该执行"})]},
            {"content": "被拒了", "calls": None},
        ]
    )
    agent = _make_agent(workspace, manager, PermissionMode.ASK, llm)
    agent.run("调用一下")
    assert any("未获用户批准" in t for t in _tool_events(agent))
    # 拒绝文本不应带上本该回显的内容（未真正发给 server）
    assert not any("不该执行" in t and "回显" not in t for t in _tool_events(agent))


def test_ask_with_approval_runs(workspace: Path, manager: McpManager) -> None:
    llm = FakeLLM(
        [
            {"content": None, "calls": [_mcp_call("mcp__fake__echo", {"text": "已批准"})]},
            {"content": "完成", "calls": None},
        ]
    )
    agent = _make_agent(workspace, manager, PermissionMode.ASK, llm, confirm=lambda _p: True)
    agent.run("调用一下")
    assert any("已批准" in t for t in _tool_events(agent))


def test_plan_mode_denies_external(workspace: Path, manager: McpManager) -> None:
    llm = FakeLLM(
        [
            {"content": None, "calls": [_mcp_call("mcp__fake__get_info", {})]},
            {"content": "不该调用", "calls": None},
        ]
    )
    agent = _make_agent(workspace, manager, PermissionMode.PLAN, llm)
    agent.run("计划调研")
    assert any("plan 模式不调用外部工具" in t for t in _tool_events(agent))


def test_full_mode_runs(workspace: Path, manager: McpManager) -> None:
    llm = FakeLLM(
        [
            {"content": None, "calls": [_mcp_call("mcp__fake__echo", {"text": "放行"})]},
            {"content": "完成", "calls": None},
        ]
    )
    agent = _make_agent(workspace, manager, PermissionMode.FULL, llm)
    agent.run("调用")
    assert any("放行" in t for t in _tool_events(agent))


# ---------------------------------------------------------------------------
# 子 agent 边界：explore 子池滤掉 EXTERNAL；run_agent 子 agent 无 MCP 工具
# ---------------------------------------------------------------------------


def test_no_mcp_env_means_no_external_tools(workspace: Path) -> None:
    env = ToolEnv(workspace=workspace, gate=None, patches=None)  # 不挂 manager
    assert not any(n.startswith("mcp__") for n in default_registry(env).names())


def test_explore_subpool_excludes_external(workspace: Path, manager: McpManager) -> None:
    from emberpy.tools.agent_tool import build_subagent_registry

    env = ToolEnv(workspace=workspace, gate=None, patches=None, mcp=manager)
    base = default_registry(env)
    assert any(n.startswith("mcp__") for n in base.names())
    explore_names = build_subagent_registry(base, "explore").names()
    assert not any(n.startswith("mcp__") for n in explore_names)


def test_spawned_subagent_has_no_mcp_tools(workspace: Path, manager: McpManager) -> None:
    sub_llm = FakeLLM([{"content": "子完成", "calls": None}])
    main_llm = FakeLLM(
        [
            {
                "content": None,
                "calls": [
                    tool_call("s1", "run_agent", {"description": "调研", "prompt": "看一下结构", "agent_type": "general"})
                ],
            },
            {"content": "父结束", "calls": None},
        ]
    )
    agent = _make_agent(
        workspace, manager, PermissionMode.AUTO, main_llm,
        allow_subagents=True, subagent_llm=sub_llm,
    )
    result = agent.run("派子 agent 去调研")
    assert result.final_content == "父结束"
    # 父工具集含 MCP 工具；子 agent 实际收到的 schemas 无 mcp__ 也无 run_agent
    assert "mcp__fake__echo" in agent.registry.names()
    assert sub_llm.complete_calls, "子 agent 应真的调了一次模型"
    child_schemas = sub_llm.complete_calls[-1][1]
    child_names = [t["function"]["name"] for t in (child_schemas or [])]
    assert not any(n.startswith("mcp__") for n in child_names)
    assert "run_agent" not in child_names
