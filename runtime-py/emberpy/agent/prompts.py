"""系统提示词。独立成模块：以后要照 claude-code-analysis 的思路
迭代 prompt（工具说明、输出格式约束、thinking 引导）只改这里。
"""
from __future__ import annotations

import re
from pathlib import Path

SYSTEM_TEMPLATE = """\
你是运行在用户项目里的编码助手 emberpy。

工作目录：{workspace}
权限模式：{mode}（plan=只读 / ask=逐条询问 / auto=自动+危险询问 / full=完全访问）

规则：
1. 先探索再行动：用 list_dir / read_file / grep 弄清项目结构和现状，不要凭空改代码。
2. 已有文件的小改动用 file_edit 精确替换；新建或整体重写用 write_file 整写。两者都会自动留下可 /undo 的 checkpoint。
3. 执行命令用 run_command（如运行测试/构建）。被权限拒绝时不要重复硬试，改为询问用户或换更安全的做法。
4. 需要向用户澄清关键取舍时用 AskUserQuestion（单选）；需要把计划/进度结构化给用户看时用 update_plan。
5. 回复用简体中文，代码与命令保持原文。
6. 完成任务后，用一段简洁的总结答复用户，说明改了什么 / 验证结果如何。
"""


def plan_mode_guide() -> str:
    """【计划模式】附加指引：只读调研 -> 结构化计划 -> 停下等批准。

    用户切到 plan 模式（/permissions plan 或权限下拉）时，本轮处于只读：写
    文件/跑非只读命令会被权限门拒绝。模型应把这段时间用于"想清楚再动手"。
    """
    return """当前是【计划模式】：只读调研，不许改文件。

规则：
1. 不要调用 write_file / file_edit / run_command（会被拒绝）。只允许 list_dir / read_file / grep 等只读工具查看项目。
2. 需求有歧义或方案选择影响用户时，先用 AskUserQuestion 问清楚；其余能查的自己去查。
3. 调研充分后，调用 update_plan 把实施步骤结构化列出（每步 {step, status:"pending"}）。
4. 然后停下，用几句话概述这个计划并等待用户批准——不要动手实现。批准后用户会发 /plan execute，届时你再逐条执行并更新每步状态。
"""


# ---------------------------------------------------------------------------
# 项目指令注入（CLAUDE.md / AGENTS.md），对齐 claude 读 CLAUDE.md 的行为
# ---------------------------------------------------------------------------
#
# claude 把 User/Project 的 CLAUDE.md 合并后作为"每轮都 prepend 的消息"给模型，
# 一次会话内是快照（只随 /clear、compact、worktree 重读）。本引擎没有 meta 消息
# 概念，等价做法是把指令拼进 system prompt 尾部（SYSTEM_TEMPLATE 之后、memory
# 之前——指令是"源"，记忆是"动态库"）。只读 workspace 层，绝不写 Session/记忆。

_INSTRUCTION_HEADING = (
    "以下是项目/用户指令（CLAUDE.md / AGENTS.md）。遵守这些指令，优先于默认行为：\n"
)
# 按序尝试的文件（workspace 相对）；claude 本身只读 CLAUDE.md，AGENTS.md 是对
# "AGENTS 生态"工具的兼容扩展（同路径存在就顺带读，优先级低于 CLAUDE.md）。
_INSTR_CANDIDATES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")
# 单文件注入上限：claude 是 40k 软上限不硬截；引擎上下文小得多，单文件超限截断
# 并提示（宁可截断不要整文件丢弃，也不要静默丢信息）。
_INSTR_MAX_FILE_CHARS = 8_000
# 进程内缓存：path -> (st_mtime_ns, st_size, 渲染文本)。每次 run() 都调用，靠
# stat 指纹判断文件是否变过，没变就复用拼好的字符串（对齐引擎 fs 的指纹惯例）。
_instruction_cache: dict[str, tuple[int, int, str]] = {}


def _strip_instr(text: str) -> str:
    """清洗 CLAUDE.md 正文：剥顶层 YAML frontmatter 与 HTML 注释（对齐 claudemd.ts）。"""
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith("---"):
        rest = stripped[3:]
        for i, line in enumerate(rest.split("\n")):
            if line.rstrip() == "---":
                stripped = "\n".join(rest.split("\n")[i + 1:]).lstrip("\n")
                break
    stripped = re.sub(r"<!--.*?-->", "", stripped, flags=re.DOTALL)
    return stripped.strip("\n")


def _render_instr(path: Path) -> str:
    """读单份指令文件并返回清洗+截断后的文本；不存在/读失败/空 => ""。"""
    key = str(path.resolve())
    try:
        st = path.stat()
    except OSError:
        _instruction_cache.pop(key, None)
        return ""
    cached = _instruction_cache.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _instruction_cache.pop(key, None)
        return ""
    text = _strip_instr(raw).strip()
    if not text:
        _instruction_cache[key] = (st.st_mtime_ns, st.st_size, "")
        return ""
    if len(text) > _INSTR_MAX_FILE_CHARS:
        text = (
            text[:_INSTR_MAX_FILE_CHARS].rstrip()
            + f"\n\n……（指令文件过长，仅注入前 {_INSTR_MAX_FILE_CHARS} 字符，可用 read_file 查看全文）"
        )
    _instruction_cache[key] = (st.st_mtime_ns, st.st_size, text)
    return text


def build_instructions(workspace: Path) -> str:
    """拼出"项目指令"整段文本；无任何指令文件时返回空串（调用方不加块）。

    只读 workspace 层的 CLAUDE.md / .claude/CLAUDE.md / AGENTS.md，逐文件渲染成
    ``Contents of <rel>:\n<正文>`` 用空行拼接。优先级：CLAUDE.md > .claude/CLAUDE.md
    > AGENTS.md（越靠后模型越注意，故主指令在前、兼容扩展在后）。注入过程完全
    绕开工具/记忆/read_state——系统读取，不登记、不写盘。
    """
    base = Path(workspace).resolve()
    blocks: list[str] = []
    for rel in _INSTR_CANDIDATES:
        text = _render_instr(base / rel)
        if text:
            blocks.append(f"Contents of {rel}:\n{text}")
    if not blocks:
        return ""
    return _INSTRUCTION_HEADING + "\n\n" + "\n\n".join(blocks) + "\n"
