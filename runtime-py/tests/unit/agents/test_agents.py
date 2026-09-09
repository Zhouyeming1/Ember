"""自定义 agent 定义文件（R-E）测试：发现 / 解析 / store / run_agent 派生子 agent。

参考 claude `tools/AgentTool/loadAgentsDir.ts`，引擎加 `.claude/agents/*.md`
（含 `.ember/agents`）目录扫描：每个定义文件 = frontmatter（name/description/
tools/disallowedTools/maxTurns/permissionMode/initialPrompt）+ 正文（当子 agent 的
system prompt）。run_agent 的 agent_type 除内置 general/explore 还能填这些名字。

覆盖：
- 发现：根下顶层 *.md；名字 = frontmatter name 否则文件 stem；description 回退正文首行；
  正文非空才收；不递归子目录；
- 解析：tools 白名单 / disallowedTools 黑名单（多行列表与单行都好）、maxTurns 数字容错、
  permissionMode/initialPrompt；filters_tools 派生；
- resolve_agent_roots：EMBERPY_AGENTS_DIR 整体覆盖、EMBER_HOME 当 home（测试隔离）、
  项目 + 用户级目录并扫、同名同真实文件去重按根顺序（项目>用户）；
- store：get/names/refresh 动态发现；
- agentic：自定义 agent -> 子 agent system prompt = 定义正文、initialPrompt 前置进任务、
  子工具池剔除 disallowedTools（可观测 schema）、maxTurns 覆盖步数上限、未知 agent_type
  回引导文本、run_agent 描述动态列出自定义名。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from emberpy.agent import Agent
from emberpy.agents import AgentStore, resolve_agent_roots
from emberpy.session import Session
from emberpy.testing import FakeLLM, tool_call
from emberpy.tools import ToolEnv
from emberpy.tools.agent_tool import SUBAGENT_TOOL_NAME


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _isolate_home(tmp_path: Path, monkeypatch) -> None:
    """把 home 指向 tmp 下独立目录：tmp 里的工作区/agent 根在它之外 -> 判为 project。"""
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EMBERPY_AGENTS_DIR", raising=False)


# ---------------------------------------------------------------------------
# 发现与解析
# ---------------------------------------------------------------------------


def test_discovers_top_level_md_and_name_rules(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(tmp_path, monkeypatch)
    root = tmp_path / "agents"
    _write(root / "reviewer.md", "---\nname: reviewer\ndescription: 代码评审\n---\n你是资深评审者。")
    _write(root / "helper.md", "---\ndescription: 助手\n---\n帮我写代码。")
    _write(root / "nested" / "ignored.md", "---\nname: 嵌套\n---\n不应被扫（非顶层）。")

    store = AgentStore([root])
    assert sorted(store.names()) == ["helper", "reviewer"]
    reviewer = store.get("reviewer")
    assert reviewer is not None
    assert reviewer.description == "代码评审"
    assert "资深评审者" in reviewer.body
    assert reviewer.source == "project"
    assert store.get("helper").name == "helper"  # 无 name -> 文件 stem
    assert store.get("nested") is None  # 不递归子目录


def test_name_desc_fallback_and_empty_body(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(tmp_path, monkeypatch)
    root = tmp_path / "agents"
    # 无 name 也无 description：name=stem，description=正文首行
    _write(root / "helper.md", "---\n---\n帮我写代码。\n第二行不算。")
    # 空正文 -> 不收（定义至少要有指令）
    _write(root / "empty.md", "---\nname: empty\n---\n  \n")
    _write(root / "ignored.txt", "---\n---\n不是 md。")

    store = AgentStore([root])
    assert sorted(store.names()) == ["helper"]
    helper = store.get("helper")
    assert helper.name == "helper"
    assert helper.description == "帮我写代码。"
    assert "第二行" in helper.body


def test_frontmatter_fields_parsed(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(tmp_path, monkeypatch)
    _write(
        tmp_path / "review.md",
        "---\n"
        "name: a-review\n"
        "description: 自动评审\n"
        "tools:\n"
        "  - read_file\n"
        "  - grep\n"
        "disallowedTools:\n"
        "  - run_command\n"
        "maxTurns: 5\n"
        "permissionMode: plan\n"
        "initialPrompt: 先读 CLAUDE.md\n"
        "---\n正文",
    )
    # 单行逗号形式也认
    _write(tmp_path / "single.md", "---\nname: s\ntools: read_file, grep\n---\n正文")

    store = AgentStore([tmp_path])
    adef = store.get("a-review")
    assert adef is not None
    assert adef.tools == ("read_file", "grep")
    assert adef.disallowed_tools == ("run_command",)
    assert adef.max_turns == 5
    assert adef.permission_mode == "plan"
    assert adef.initial_prompt == "先读 CLAUDE.md"
    assert adef.filters_tools is True
    assert adef.body == "正文"
    s = store.get("s")
    assert s is not None
    assert s.tools == ("read_file", "grep")


def test_bad_int_max_turns_tolerated(tmp_path: Path) -> None:
    _write(tmp_path / "bad.md", "---\nname: bad\nmaxTurns: 很多\n---\n正文")
    (adef,) = AgentStore([tmp_path]).all()
    assert adef.max_turns == 0


# ---------------------------------------------------------------------------
# resolve_agent_roots / store
# ---------------------------------------------------------------------------


def test_resolve_override_env(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "my-agents"
    target.mkdir()
    monkeypatch.setenv("EMBERPY_AGENTS_DIR", str(target))
    assert resolve_agent_roots(tmp_path / "proj") == [target.resolve()]
    monkeypatch.setenv("EMBERPY_AGENTS_DIR", str(tmp_path / "nope"))
    assert resolve_agent_roots(tmp_path / "proj") == []


def test_resolve_project_and_user_and_project_wins(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "proj"
    proj_agents = ws / ".claude" / "agents"
    user_agents = tmp_path / "home" / ".claude" / "agents"
    _write(proj_agents / "proj-agent.md", "---\n---\n项目角色。")
    _write(proj_agents / "dup.md", "---\nname: share\ndescription: 项目版\n---\n项目正文。")
    _write(user_agents / "user-agent.md", "---\n---\n用户角色。")
    _write(user_agents / "dup.md", "---\nname: share\ndescription: 用户版\n---\n用户正文。")

    store = AgentStore(resolve_agent_roots(ws))
    assert sorted(store.names()) == ["proj-agent", "share", "user-agent"]
    # 同名冲突按根顺序：项目（先扫）赢
    assert store.get("share").source == "project"
    assert store.get("proj-agent").source == "project"
    assert store.get("user-agent").source == "user"
    # listing 只读字符串，含两条真实定义（同名两条都列出，冲突只影响 get/names）
    listing = store.listing()
    assert "proj-agent" in listing and "user-agent" in listing


def test_store_refresh_discovers_new_and_removed(tmp_path: Path) -> None:
    _write(tmp_path / "old.md", "---\nname: old\n---\n旧。")
    store = AgentStore([tmp_path])
    assert store.get("new") is None
    _write(tmp_path / "new.md", "---\nname: new\n---\n新。")
    (tmp_path / "old.md").unlink()
    store.refresh()
    assert store.get("new") is not None
    assert store.get("old") is None


# ---------------------------------------------------------------------------
# agentic：run_agent 按自定义角色派生子 agent
# ---------------------------------------------------------------------------


class SpyLLM(FakeLLM):
    """FakeLLM + 录制每次 complete 的消息与工具 schema（验证子 agent 上下文/工具池）。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.all_calls: list[list[dict[str, Any]]] = []
        self.all_tools: list[list[str]] = []

    def complete(self, messages, tools=None):
        self.all_calls.append(list(messages))
        self.all_tools.append([t["function"]["name"] for t in (tools or [])])
        return super().complete(messages, tools)


def _make_agent(
    script: list[dict[str, Any]],
    workspace: Path,
    store: Optional[AgentStore],
    *,
    subagent_llm: Optional[FakeLLM] = None,
) -> Agent:
    env = ToolEnv(workspace=workspace, gate=None, patches=None, agents=store)
    return Agent(
        llm=FakeLLM(script),
        workspace=workspace,
        mode="auto",
        env=env,
        session=Session(cwd=workspace),
        allow_subagents=True,
        subagent_llm=subagent_llm,
    )


def test_custom_agent_body_and_initial_prompt(workspace: Path, tmp_path: Path) -> None:
    _write(
        tmp_path / "agents" / "reviewer.md",
        "---\n"
        "name: reviewer\n"
        "description: 代码评审\n"
        "tools: read_file, grep, list_dir, glob\n"
        "maxTurns: 3\n"
        "permissionMode: plan\n"
        "initialPrompt: 只做只读评审，别改文件\n"
        "---\n"
        "你是资深代码评审者。",
    )
    store = AgentStore([tmp_path / "agents"])
    sub_llm = SpyLLM([{"content": "评审通过，无问题。", "calls": None}])
    agent = _make_agent(
        [
            {
                "content": None,
                "calls": [
                    tool_call(
                        "s1",
                        SUBAGENT_TOOL_NAME,
                        {"description": "评审代码", "prompt": "请评审 src/app.py", "agent_type": "reviewer"},
                    )
                ],
            },
            {"content": "完成评审。", "calls": None},
        ],
        workspace,
        store,
        subagent_llm=sub_llm,
    )
    result = agent.run("帮我评审 src/app.py")
    assert result.final_content == "完成评审。"

    # 子 agent 上下文：[system=定义正文, user=initialPrompt + \n\n + prompt]
    assert len(sub_llm.all_calls) == 1
    system = sub_llm.all_calls[0][0]
    assert system["role"] == "system"
    assert "资深代码评审者" in system["content"]
    user_content = sub_llm.all_calls[0][-1]["content"]
    assert user_content.startswith("只做只读评审，别改文件")
    assert "请评审 src/app.py" in user_content
    # 子 agent 的工具池受 tools 白名单约束（且不能递归派生）
    assert set(sub_llm.all_tools[0]) == {"read_file", "grep", "list_dir", "glob"}


def test_custom_agent_disallowed_tools_removed(workspace: Path, tmp_path: Path) -> None:
    _write(
        tmp_path / "agents" / "looker.md",
        "---\nname: looker\ndescription: 只看不写\npermissionMode: plan\n"
        "disallowedTools: run_command, web_search, fetch_content\n---\n你只读。",
    )
    store = AgentStore([tmp_path / "agents"])
    sub_llm = SpyLLM([{"content": "结构如下……", "calls": None}])
    agent = _make_agent(
        [
            {
                "content": None,
                "calls": [tool_call("t1", SUBAGENT_TOOL_NAME, {"description": "读", "prompt": "看一下", "agent_type": "looker"})],
            },
            {"content": "看完了", "calls": None},
        ],
        workspace,
        store,
        subagent_llm=sub_llm,
    )
    result = agent.run("帮我看看项目结构")
    assert result.final_content == "看完了"

    names = set(sub_llm.all_tools[0])
    assert "read_file" in names and "write_file" in names  # 只剔黑名单，不整体锁只读
    assert "run_command" not in names
    assert "web_search" not in names
    assert "fetch_content" not in names
    assert SUBAGENT_TOOL_NAME not in names  # 子 agent 不能递归派生


def test_custom_agent_max_turns_enforced(workspace: Path, tmp_path: Path) -> None:
    _write(tmp_path / "agents" / "tiny.md", "---\nname: tiny\ndescription: 一步就停\nmaxTurns: 1\n---\n快。")
    store = AgentStore([tmp_path / "agents"])
    # 子 agent 第一轮要工具；max_turns=1 意味着工具执行后步数到顶 -> 不再发第二轮
    sub_llm = SpyLLM(
        [
            {"content": None, "calls": [tool_call("a1", "list_dir", {"path": "."})]},
            {"content": "还想再跑", "calls": None},
        ]
    )
    agent = _make_agent(
        [
            {
                "content": None,
                "calls": [tool_call("s1", SUBAGENT_TOOL_NAME, {"description": "快速", "prompt": "列个目录", "agent_type": "tiny"})],
            },
            {"content": "收工", "calls": None},
        ],
        workspace,
        store,
        subagent_llm=sub_llm,
    )
    result = agent.run("跑一下")
    assert result.final_content == "收工"
    # 步数上限 1 -> 只发了一轮（工具调用那轮），第二条脚本没有被消费
    assert len(sub_llm.all_calls) == 1


def test_run_agent_unknown_custom_type_gives_guidance(workspace: Path) -> None:
    # 没有自定义 agent 定义 -> 未知 agent_type 回引导文本，主流程照常继续
    agent = _make_agent(
        [
            {
                "content": None,
                "calls": [tool_call("s1", SUBAGENT_TOOL_NAME, {"description": "x", "prompt": "y", "agent_type": "ghost"})],
            },
            {"content": "完", "calls": None},
        ],
        workspace,
        None,
    )
    result = agent.run("任务")
    assert result.final_content == "完"


def test_run_agent_schema_lists_custom_names(workspace: Path, tmp_path: Path) -> None:
    _write(tmp_path / "agents" / "x1.md", "---\nname: auditor\ndescription: 审计\n---\n审计。")
    store = AgentStore([tmp_path / "agents"])
    agent = _make_agent([], workspace, store)
    tool = agent.registry.get(SUBAGENT_TOOL_NAME)
    assert tool is not None
    desc = tool.schema()["function"]["description"]
    assert "auditor" in desc  # 自定义 agent 名动态进描述
    assert "general" in desc and "explore" in desc  # 内置仍在
