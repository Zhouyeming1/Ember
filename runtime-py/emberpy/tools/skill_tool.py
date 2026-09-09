"""Skill 工具：让模型把一段技能工作流拉进上下文并照做（模型驱动技能执行）。

与 claude 的 Skill 工具同思路：工具描述里带上"技能清单"，模型在任务匹配某技能
时用 ``skill=<名字>, args=<参数>`` 调用。执行方式按技能的执行上下文分两种：
- 常规技能：工具把渲染后的正文（含 ``$ARGUMENTS`` 插值 + base-dir 头）作为结果
  回给模型，模型随之下一步自己照正文执行（inline 注入）。正文因此成为一段"本条
  助手轮次需要遵守的工作流"。
- ``context: fork`` 技能（fork=True）：有 spawner（allow_subagents 的主 agent）时
  把渲染正文当 prompt 交给 **general 子 agent 隔离执行**（对齐 claude 的
  executeForkedSkill：独立子上下文、自用 token 预算、正文不进主历史），工具结果
  就是子 agent 的最终答复——主上下文只见"一条 Skill 工具轨迹 + 它的结论"，不会被
  长工作流正文污染。子 agent 环境（env.spawner 为 None，或父没开 allow_subagents）
  回退 inline，正文当工作流注入主上下文照常执行。

安全：本工具只读已发现目录里的技能 md（名字由 store 校验），不执行正文里的任何
命令（inline shell 被有意去掉——要跑命令走 shell 工具含权限门）；fork 技能的
正文是作为纯文本 prompt 交给子 agent，同样不经 shell 解释。disable-model-
invocation 的技能模型调了也拒绝。category=READ：只读技能库，不经写权限门。
"""
from __future__ import annotations

from typing import Any

from ..skills import SkillStore
from .agent_tool import AGENT_TYPE_GENERAL
from .registry import Tool, ToolCategory, ToolEnv


def build_skill_tool(env: ToolEnv) -> Tool:
    """按 env 构造 Skill 工具。store 取 env.skills；fork 执行看 env.spawner。

    收 env 而非 store：fn 每次被调用时才读 env.spawner，这样 Agent.__init__
    在 default_registry **之前**注入的 spawner 也能被感知，同一 Agent 内无需重建工具。
    """
    store = env.skills
    assert store is not None, "build_skill_tool 需 env.skills 已挂上 SkillStore"

    def _describe() -> str:
        # R-F：description 每次 schema() 求值 -> 条件技能被触碰激活后，下一步模型
        # 就能在工具描述里看到它（agent.run 每步重算 schemas，无需重建注册表）。
        return (
            "调用一个技能（skill）：技能是一段现成的专业工作流说明，把它加载进上下文"
            "后你按它执行。任务匹配某技能时用它，比凭空自由发挥更稳。用法："
            'skill=<技能名>, args=<传给技能正文 $ARGUMENTS 的参数文本，可省>。\n'
            "标记 context: fork 的技能会在独立子 agent 里执行完，工具结果直接是它"
            "的最终答复（正文不展开到本对话），照常概括进你的答复。\n"
            "可用技能：\n"
            + store.listing()
        )

    def run(skill: str, args: str = "") -> str:
        found = store.get(skill)
        if found is None:
            avail = "、".join(s.name for s in store.available()) or "（无）"
            return (
                f"没有技能 {skill!r}。可用技能：{avail}\n"
                "请用正确的 skill 名重试，或不要调技能直接干活。"
            )
        if found.disable_model_invocation:
            return f"技能 {found.name} 标了 disable-model-invocation，模型不能直接调用（请向用户说明或走 /skill: 命令面）。"

        if found.fork and env.spawner is not None:
            # fork：渲染正文当 prompt 交给 general 子 agent 隔离执行，回最终答复。
            body = store.render(found, args)
            description = f"执行 context: fork 的技能「{found.name}」"
            try:
                child_reply = (env.spawner(description, body, AGENT_TYPE_GENERAL) or "").strip()
            except Exception as exc:
                return f"隔离执行技能「{found.name}」失败：{exc}"
            if not child_reply:
                return (
                    f"技能「{found.name}」（context: fork）已在独立子 agent 中跑完，"
                    "但没有产出文本答复（它可能只完成了文件改动）。请检查文件状态。"
                )
            return (
                f"技能「{found.name}」标记为 context: fork，已把正文交给一个独立子 agent"
                f"隔离执行完（它的工具轨迹不进本对话），最终答复如下：\n\n{child_reply}"
            )

        content = store.render(found, args)
        where = ""
        if found.base_dir is not None:
            where = f"\n技能所在目录：`{found.base_dir}`（正文里的相对路径引用以此为准）"
        return (
            f"已加载技能「{found.name}」。下面是要你遵循的工作流/说明，照它执行；"
            f"需要写文件或跑命令时走对应的写文件/命令工具（仍受权限门约束）。{where}\n\n{content}"
        )

    return Tool(
        name="Skill",
        description=_describe,
        parameters={
            "properties": {
                "skill": {"type": "string", "description": "技能名（见上方清单；命名空间技能形如 a:b）"},
                "args": {"type": "string", "description": "参数文本，插进技能正文的 $ARGUMENTS（可省）"},
            },
            "required": ["skill"],
        },
        category=ToolCategory.READ,
        fn=run,
    )
