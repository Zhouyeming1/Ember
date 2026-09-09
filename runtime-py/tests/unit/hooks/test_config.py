"""hooks 配置加载单元测试。

覆盖：现代/简写两种文件形状、matcher 解析、未知事件告警、非 command 类型记账忽略、
坏文件容错、项目级 env 门控（EMBERPY_PROJECT_HOOKS=1）、matcher 匹配语义。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.hooks import HookManager
from emberpy.hooks.runner import quote_command

import sys


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _loader(workspace: Path, user_path: Path) -> HookManager:
    return HookManager.load(workspace, user_path=user_path)


def test_modern_shape_with_hooks_wrapper(tmp_path: Path) -> None:
    hook_file = _write(
        tmp_path / "hooks.json",
        '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}',
    )
    mgr = _loader(tmp_path, hook_file)
    assert mgr.has("Stop")
    assert not mgr.has("PreToolUse")
    rules = mgr.rules_for("Stop")
    assert len(rules) == 1 and rules[0].command == "echo bye"


def test_unknown_event_and_non_command_warn(tmp_path: Path) -> None:
    hook_file = _write(
        tmp_path / "hooks.json",
        '{"NotAnEvent": [{"type":"command","command":"x"}],'
        ' "PreToolUse": [{"type":"prompt","command":"y"}, {"type":"command","command":"z"}]}',
    )
    mgr = _loader(tmp_path, hook_file)
    assert not mgr.has("NotAnEvent")
    # 只有 command 类型的被收下；prompt 类型被忽略并告警
    assert len(mgr.rules_for("PreToolUse")) == 1
    assert mgr.rules_for("PreToolUse")[0].command == "z"
    joined = "\n".join(mgr.warnings)
    assert "NotAnEvent" in joined and "prompt" in joined


def test_session_end_is_known_event(tmp_path: Path) -> None:
    """SessionEnd 必须在白名单里（worker 在发但曾被当未知事件丢弃 -> 永不触发）。"""
    hook_file = _write(
        tmp_path / "hooks.json",
        '{"SessionEnd": [{"type": "command", "command": "echo bye"}]}',
    )
    mgr = _loader(tmp_path, hook_file)
    assert mgr.has("SessionEnd")
    assert len(mgr.rules_for("SessionEnd")) == 1
    assert not any("SessionEnd" in w for w in mgr.warnings)


def test_bad_json_and_missing_file_tolerated(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.json", "{ 这不是 JSON")
    mgr = _loader(tmp_path, bad)
    assert not mgr.has("Stop")
    assert any("读取" in w or "JSON" in w for w in mgr.warnings)

    missing = tmp_path / "missing.json"
    mgr2 = _loader(tmp_path, missing)
    assert not mgr2.has("Stop")
    assert mgr2.warnings == []


def test_project_hooks_gated_by_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EMBERPY_PROJECT_HOOKS", raising=False)
    user = _write(tmp_path / "user.json", '{"Stop": [{"type":"command","command":"u"}]}')
    project_file = _write(
        tmp_path / ".ember" / "hooks.json",
        '{"PreToolUse": [{"type":"command","command":"p"}]}',
    )
    # 默认：项目级不并入（只有用户级）
    off = HookManager.load(tmp_path, user_path=user, project_path=project_file)
    assert off.has("Stop") and not off.has("PreToolUse")
    # 显式开启：项目级并入
    on = HookManager.load(tmp_path, user_path=user, project_path=project_file, project_enabled=True)
    assert on.has("Stop") and on.has("PreToolUse")
    # env 开关同样生效
    monkeypatch.setenv("EMBERPY_PROJECT_HOOKS", "1")
    via_env = HookManager.load(tmp_path, user_path=user, project_path=project_file)
    assert via_env.has("PreToolUse")


def test_matcher_matching_semantics(tmp_path: Path) -> None:
    hook_file = _write(
        tmp_path / "hooks.json",
        '{"PreToolUse": [{"matcher": "write_file|file_edit",'
        ' "hooks": [{"type": "command", "command": "echo x"}]}]}',
    )
    mgr = _loader(tmp_path, hook_file)
    rule = mgr.rules_for("PreToolUse")[0]
    assert rule.matches("write_file") is True
    assert rule.matches("file_edit") is True
    assert rule.matches("read_file") is False
    assert rule.matcher is not None


def test_default_home_resolution_reads_home_hooks(tmp_path: Path, monkeypatch) -> None:
    """未显式传 user_path 时，按 EMBER_HOME/.ember/hooks.json 读取（测试隔离）。"""
    home = tmp_path / "home"
    hook_file = _write(home / ".ember" / "hooks.json", '{"Stop": [{"type":"command","command":"s"}]}')
    monkeypatch.setenv("EMBER_HOME", str(home))
    monkeypatch.delenv("EMBERPY_PROJECT_HOOKS", raising=False)
    mgr = HookManager.load(tmp_path / "proj")
    assert mgr.has("Stop")
    assert hook_file.exists()
    assert "proj" not in str(hook_file)


def test_quote_roundtrip() -> None:
    """quote_command 与 split_command 语义互逆（Windows/POSIX 一致地保住路径反斜杠）。"""
    from emberpy.hooks.runner import split_command

    arg = "C:\\Users\\liu\\工具 dir\\hook.py"
    cmd = quote_command(sys.executable, arg, "-c", "print(1)")
    tokens = split_command(cmd)
    assert tokens[0] == sys.executable
    assert tokens[1] == arg
    assert tokens[2] == "-c"
