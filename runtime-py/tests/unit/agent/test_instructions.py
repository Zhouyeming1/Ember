"""CLAUDE.md / AGENTS.md 项目指令注入单元测试（R-A）。

覆盖：无文件返回空串、主指令注入、优先级顺序、frontmatter/HTML 注释清洗、
空文件跳过、超限截断提示、mtime 指纹缓存、注入进 system prompt 而不进会话历史。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.agent.prompts import _INSTRUCTION_HEADING, build_instructions
from emberpy.testing import make_agent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_no_instructions_returns_empty(workspace: Path) -> None:
    assert build_instructions(workspace) == ""


def test_injects_claude_md(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "不要动 docs/ 目录。")
    text = build_instructions(workspace)
    assert _INSTRUCTION_HEADING.strip() in text
    assert "Contents of CLAUDE.md:" in text
    assert "不要动 docs/ 目录。" in text


def test_order_claude_then_claude_dir_then_agents(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "主指令")
    _write(workspace / ".claude" / "CLAUDE.md", "次指令")
    _write(workspace / "AGENTS.md", "生态扩展")
    text = build_instructions(workspace)
    assert text.index("主指令") < text.index("次指令") < text.index("生态扩展")


def test_frontmatter_and_html_stripped(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "---\nname: rules\n---\n<!-- 注释 -->\n正文")
    text = build_instructions(workspace)
    assert "name: rules" not in text
    assert "注释" not in text
    assert "正文" in text


def test_whitespace_only_file_skipped(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "   \n  ")
    assert build_instructions(workspace) == ""


def test_long_file_truncated_with_hint(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "x" * 9000)
    text = build_instructions(workspace)
    assert len(text) < 9000
    assert "过长" in text


def test_mtime_fingerprint_picks_up_edit(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "版本一")
    assert "版本一" in build_instructions(workspace)
    _write(workspace / "CLAUDE.md", "版本二更长内容。")
    assert "版本二" in build_instructions(workspace)


def test_system_prompt_includes_instructions_not_history(workspace: Path) -> None:
    _write(workspace / "CLAUDE.md", "本项目永远别提交 secrets。")
    agent = make_agent([{"content": "ok", "calls": None}], workspace, mode="auto")
    content = agent._system_prompt()["content"]
    assert "别提交 secrets" in content
    # 指令只进 system prompt，不进会话历史（不污染 /undo / 落盘语义）
    assert agent.session.messages() == []
