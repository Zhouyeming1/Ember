"""run_agent 子 agent 单元测试：工具开关 / 同步派生 / explore 只读 / 共享 undo。

行为参考 claude AgentTool 同步路径（子 agent 独立会话、取最后一条 assistant 文本
回父、子 agent 不能再派生），代码自写。用能录制消息的 spy LLM 断言数据流。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from emberpy.tools import ToolEnv, default_registry
from emberpy.tools.agent_tool import SUBAGENT_TOOL_NAME, build_subagent_registry
from emberpy.testing import FakeLLM, make_agent, tool_call


class SpyLLM(FakeLLM):
    """FakeLLM + 录制每次 complete 的完整消息（验证注入/上下文形状用）。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.all_calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: Optional[list] = None):
        self.all_calls.append(list(messages))
        return super().complete(messages, tools)


def _roles(llm: SpyLLM) -> list[str]:
    return [m["role"] for m in llm.all_calls[0]]


# ---------------------------------------------------------------------------
# 工具开关
# ---------------------------------------------------------------------------


def test_run_agent_tool_only_when_enabled(workspace: Path) -> None:
    off = make_agent([], workspace)
    assert SUBAGENT_TOOL_NAME not in off.registry.names()

    on = make_agent([], workspace, allow_subagents=True)
    tool = on.registry.get(SUBAGENT_TOOL_NAME)
    assert tool is not None
    schema = tool.schema()
    props = schema["function"]["parameters"]["properties"]
    assert "description" in props and "prompt" in props
    # agent_type 是自由字符串（可选值随自定义 agent 定义动态变化，R-E），
    # description 里枚举内置 general/explore 取值
    assert props["agent_type"]["type"] == "string"
    assert "general" in schema["function"]["description"]
    assert "explore" in schema["function"]["description"]


# ---------------------------------------------------------------------------
# general 子 agent：独立会话 + 最终文本回父
# ---------------------------------------------------------------------------


def test_general_subagent_runs_and_result_injects_back(workspace: Path) -> None:
    sub_llm = SpyLLM([{"content": "子结果：已把 abc 处理完", "calls": None}])
    main_llm = SpyLLM(
        [
            {
                "content": None,
                "calls": [tool_call("s1", SUBAGENT_TOOL_NAME,
                                     {"description": "处理 abc", "prompt": "请处理 abc 并汇报", "agent_type": "general"})],
            },
            {"content": "主答复：子 agent 已完成", "calls": None},
        ]
    )
    agent = make_agent([], workspace, allow_subagents=True, llm=main_llm, subagent_llm=sub_llm)
    result = agent.run("主任务")

    # 子 agent 只被调一次，且它的上下文 = [system, user(prompt)]（看不到父历史）
    assert len(sub_llm.all_calls) == 1
    assert _roles(sub_llm) == ["system", "user"]
    assert sub_llm.all_calls[0][-1]["content"] == "请处理 abc 并汇报"

    # 父第二回合（最终答复）的消息里出现了子 agent 最终文本作为 tool 结果
    parent_final = main_llm.all_calls[-1]
    tool_contents = [m["content"] for m in parent_final if m["role"] == "tool"]
    assert any("子结果：已把 abc 处理完" in c for c in tool_contents)

    assert result.final_content == "主答复：子 agent 已完成"


# ---------------------------------------------------------------------------
# explore：子 agent 工具池只读（无 write/shell），仍能正常跑
# ---------------------------------------------------------------------------


def test_explore_registry_is_read_only(workspace: Path) -> None:
    env = ToolEnv(workspace=workspace, gate=None, patches=None)
    base = default_registry(env)
    # default_registry 本身不含 run_agent
    assert SUBAGENT_TOOL_NAME not in base.names()

    explore = build_subagent_registry(base, "explore").names()
    assert "read_file" in explore and "list_dir" in explore
    assert "write_file" not in explore
    assert "file_edit" not in explore
    assert "run_command" not in explore

    general = build_subagent_registry(base, "general").names()
    assert "write_file" in general and "run_command" in general
    assert SUBAGENT_TOOL_NAME not in general  # 子 agent 不能递归派生


def test_explore_subagent_runs_under_read_only_registry(workspace: Path) -> None:
    sub_llm = SpyLLM([{"content": "调研完成", "calls": None}])
    main_llm = SpyLLM(
        [
            {
                "content": None,
                "calls": [tool_call("s1", SUBAGENT_TOOL_NAME,
                                     {"description": "只读调研", "prompt": "读一下结构", "agent_type": "explore"})],
            },
            {"content": "主答复：调研完毕", "calls": None},
        ]
    )
    agent = make_agent([], workspace, allow_subagents=True, llm=main_llm, subagent_llm=sub_llm)
    result = agent.run("去调研")
    assert len(sub_llm.all_calls) == 1
    assert result.final_content == "主答复：调研完毕"


# ---------------------------------------------------------------------------
# 共享 PatchStore：子 agent 写文件进同一个 /undo 账本
# ---------------------------------------------------------------------------


def test_subagent_file_write_lands_in_shared_patches(workspace: Path) -> None:
    workspace.joinpath("x.txt").write_text("old\n", encoding="utf-8")
    sub_llm = SpyLLM(
        [
            {"content": None, "calls": [tool_call("c0", "read_file", {"path": "x.txt"})]},
            {
                "content": None,
                "calls": [tool_call("c1", "write_file", {"path": "x.txt", "content": "new content\n"})],
            },
            {"content": "已改写 x.txt", "calls": None},
        ]
    )
    main_llm = SpyLLM(
        [
            {
                "content": None,
                "calls": [tool_call("s1", SUBAGENT_TOOL_NAME,
                                     {"description": "改文件", "prompt": "把 x.txt 内容改掉", "agent_type": "general"})],
            },
            {"content": "完成", "calls": None},
        ]
    )
    agent = make_agent([], workspace, allow_subagents=True, llm=main_llm, subagent_llm=sub_llm)
    agent.run("改文件")
    assert workspace.joinpath("x.txt").read_text(encoding="utf-8") == "new content\n"
    # 子 agent 的写文件补丁落在父共享的 PatchStore -> /undo 能回滚
    assert len(agent.patches.since_seq(0)) == 1
    patch = agent.patches.since_seq(0)[0]
    assert "x.txt" in str(patch.path)
