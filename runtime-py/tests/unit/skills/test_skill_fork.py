"""context: fork 技能 + 动态发现 测试。

覆盖：
- loader：context: fork -> skill.fork；缺省/其它值 -> False；
- fork 工具（有 spawner）：正文当 prompt 交给 general 子 agent 隔离执行，父上下文
  只见一条 Skill 工具轨迹与子 agent 的最终答复，**正文不进父历史**；子 agent 的
  文件改动进共享 PatchStore（/undo 账本）；
- fork 工具（无 spawner，子 agent 环境）：回退 inline，正文当工作流注入；
- SkillStore.refresh()：磁盘上新增/删除的技能对同一 store 即时可见；
- Agent.run 开头会刷新 env.skills（同一会话续跑间新技能可被感知）。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.agent import Agent
from emberpy.session import Session
from emberpy.skills import SkillStore
from emberpy.testing import FakeLLM, tool_call
from emberpy.tools import ToolEnv, default_registry
from emberpy.tools.skill_tool import build_skill_tool


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fork_skill_root(root: Path) -> Path:
    """造一个 context: fork 的技能「gen」：正文带只在子上下文出现的标记行。"""
    _write(
        root / "gen" / "SKILL.md",
        "---\nname: gen\ndescription: 生成报告\ncontext: fork\n---\n"
        "【机密正文-仅子上下文】请在工作区根目录写文件 report.txt，"
        "内容为“由子代理产出的报告”。完成后用一句话汇报你写了什么。",
    )
    return root


def _event_texts(events: list[dict]) -> list[str]:
    """把事件序列里的全部消息正文抽成字符串列表（查正文是否泄漏用）。"""
    out: list[str] = []
    for event in events:
        message = event.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        out.append(text)
    return out


# ---------------------------------------------------------------------------
# loader：context 解析
# ---------------------------------------------------------------------------


def test_context_fork_parsed(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "SKILL.md", "---\ncontext: fork\n---\n甲。")
    _write(tmp_path / "b" / "SKILL.md", "---\ndescription: 普通\n---\n乙。")
    _write(tmp_path / "c" / "SKILL.md", "---\ncontext: sidecar\n---\n丙。")
    store = SkillStore([tmp_path])
    assert store.get("a").fork is True
    assert store.get("b").fork is False
    assert store.get("c").fork is False  # 只有 'fork' 这个值才隔离


# ---------------------------------------------------------------------------
# fork 工具：有无 spawner 两条路径
# ---------------------------------------------------------------------------


def test_fork_with_spawner_runs_in_subagent_not_inline(tmp_path: Path) -> None:
    _fork_skill_root(tmp_path)
    env = ToolEnv(
        workspace=tmp_path,
        gate=None,
        patches=None,
        skills=SkillStore([tmp_path]),
    )
    # 假 spawner：记下收到的 prompt，回一条"子代理结论"当最终答复
    received: dict[str, str] = {}

    def fake_spawner(description: str, prompt: str, agent_type: str) -> str:
        received["prompt"] = prompt
        received["agent_type"] = agent_type
        return "子代理结论：已在 report.txt 写入报告。"

    env.spawner = fake_spawner  # type: ignore[assignment]
    tool = build_skill_tool(env)

    out = tool.fn(skill="gen")
    # fork 路径：回的是子代理最终答复，不是正文注入
    assert "子代理结论：已在 report.txt 写入报告。" in out
    assert "【机密正文-仅子上下文】" not in out
    # spawner 收到的是渲染后的正文，agent 类型是 general
    assert "【机密正文-仅子上下文】" in received["prompt"]
    assert received["agent_type"] == "general"


def test_fork_without_spawner_falls_back_inline(tmp_path: Path) -> None:
    _fork_skill_root(tmp_path)
    env = ToolEnv(
        workspace=tmp_path,
        gate=None,
        patches=None,
        skills=SkillStore([tmp_path]),
        spawner=None,
    )
    tool = build_skill_tool(env)
    out = tool.fn(skill="gen")
    # 无 spawner（子 agent 环境/没开 allow_subagents）：正文当工作流注入
    assert "已加载技能「gen」" in out
    assert "【机密正文-仅子上下文】" in out


# ---------------------------------------------------------------------------
# agentic 端到端：正文不进父历史、写文件进共享 PatchStore
# ---------------------------------------------------------------------------


def test_fork_skill_end_to_end_isolated(workspace: Path) -> None:
    _fork_skill_root(workspace)
    store = SkillStore([workspace])
    assert store.get("gen").fork is True

    env = ToolEnv(
        workspace=workspace,
        gate=None,
        patches=None,
        skills=store,
    )
    main_llm = FakeLLM([
        {"content": None, "calls": [tool_call("c1", "Skill", {"skill": "gen"})]},
        {"content": "已让子代理生成报告，我把结论告诉你了。", "calls": None},
    ])
    # 子 agent 的脚本：先写文件，再一句话汇报（正文由 fork 路径当 prompt 注入）
    child_llm = FakeLLM([
        {"content": None, "calls": [tool_call("c1", "write_file", {"path": "report.txt", "content": "由子代理产出的报告\n"})]},
        {"content": "报告已写入 report.txt。", "calls": None},
    ])

    agent = Agent(
        llm=main_llm,
        workspace=workspace,
        mode="auto",
        env=env,
        session=Session(cwd=workspace),
        allow_subagents=True,
        subagent_llm=child_llm,
    )
    result = agent.run("用 gen 技能生成一份报告")

    # 父 agent 正常收尾
    assert result.final_content == "已让子代理生成报告，我把结论告诉你了。"
    # 子代理真跑了一次（父 2 次模型调用 + 子 2 次）
    assert len(main_llm.complete_calls) == 2
    assert len(child_llm.complete_calls) == 2

    # 子代理写文件落盘成功
    report = workspace / "report.txt"
    assert report.exists() and "由子代理产出的报告" in report.read_text(encoding="utf-8")

    # 子代理的文件改动进了共享 PatchStore -> 父 agent 能 /undo 回滚
    patch = agent.patches.undo_last()
    assert patch is not None and "report.txt" in patch.summary
    assert not report.exists()  # 回滚后文件被删除

    # 隔离关键断言：技能正文/文件内容标记绝不进父上下文的历史
    texts = "\n".join(_event_texts(agent.session.events()))
    assert "【机密正文-仅子上下文】" not in texts
    assert "由子代理产出的报告" not in texts
    # 但子代理的结论（Skill 工具结果）如实可见
    assert "报告已写入 report.txt。" in texts


# ---------------------------------------------------------------------------
# 动态发现
# ---------------------------------------------------------------------------


def test_store_refresh_sees_new_and_removed_skills(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "SKILL.md", "---\n---\n技能甲。")
    store = SkillStore([tmp_path])
    assert store.get("a") is not None
    assert store.get("b") is None

    # 运行中新写了技能 b -> refresh 后可见
    _write(tmp_path / "b" / "SKILL.md", "---\n---\n技能乙。")
    assert store.get("b") is None  # 未 refresh 前不可见
    store.refresh()
    assert store.get("b") is not None
    assert "b" in store.names()

    # 技能 b 被删除 -> refresh 后消失（a 不受影响）
    (tmp_path / "b" / "SKILL.md").unlink()
    store.refresh()
    assert store.get("b") is None
    assert store.get("a") is not None


def test_agent_run_start_refreshes_skills(workspace: Path) -> None:
    """Agent.run 开头会对 env.skills 调 refresh（动态发现挂点）。"""
    refreshed: list[int] = []

    class _SpySkills:
        def all(self) -> list:
            return []

        def refresh(self) -> None:
            refreshed.append(1)

    env = ToolEnv(workspace=workspace, gate=None, patches=None, skills=_SpySkills())  # type: ignore[arg-type]
    agent = Agent(
        llm=FakeLLM([
            {"content": "第一轮答复", "calls": None},
            {"content": "第二轮答复", "calls": None},
        ]),
        workspace=workspace,
        mode="auto",
        env=env,
        session=Session(cwd=workspace),
    )
    agent.run("任务一")
    agent.run("任务二")
    assert len(refreshed) == 2  # 每轮开始都刷新一次
    # 空的技能库不该把 Skill 工具挂进来
    assert "Skill" not in agent.registry.names()
    assert default_registry(env).names()  # 空 skills 仍能正常建默认注册表
