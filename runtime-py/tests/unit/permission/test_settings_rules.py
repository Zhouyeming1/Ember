"""Feature A（2026-09）：.claude/settings.json 工具级 allow/deny 规则。

覆盖：
- 规则解析（裸 Tool / Tool(pattern) / 坏条目静默丢弃）
- 引擎名 ↔ claude CamelCase 别名映射
- 前缀 / 通配匹配语义
- 安全不变量：deny 无条件（压过 full/confirm/allow）；allow 只豁免普通询问，
  不豁免敏感写（.env/.git…）与 deny；硬拦截不受 allow 影响
- env 门控：EMBERPY_PROJECT_SETTINGS 关 -> 忽略文件；坏/缺 JSON -> 空规则
- 与路径 deny（EMBERPY_DENY / .ember/permissions.json）并存
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from emberpy.errors import Denied
from emberpy.permission import (
    PermissionGate,
    PermissionMode,
    PermissionPolicy,
    load_permission_policy,
)


def _pol(
    *,
    ws: Path | None = None,
    tool_deny: list | None = None,
    tool_allow: list | None = None,
    deny_patterns: list | None = None,
) -> PermissionPolicy:
    return PermissionPolicy(
        ws or Path("/ws"),
        deny_patterns,
        tool_deny=tool_deny,
        tool_allow=tool_allow,
    )


def _set_tool(g: PermissionGate, tool_name: str) -> PermissionGate:
    g.request_context = {"tool_name": tool_name, "tool_input": {}}
    return g


# -- 规则解析 -------------------------------------------------------------
class TestRuleParse:
    def test_bare_tool_matches_any_input(self) -> None:
        p = _pol(tool_deny=["run_command"])
        assert p.denies_tool("run_command", "随便什么命令都命中")
        assert p.denies_tool("run_command", None)  # spec 为 None 也命中（裸工具）

    def test_paren_pattern_matches(self) -> None:
        p = _pol(tool_deny=["Bash(rm -rf *)"])
        assert p.denies_tool("run_command", "rm -rf build")
        assert not p.denies_tool("run_command", "python -m pytest")

    def test_invalid_entries_dropped_silently(self) -> None:
        p = _pol(tool_deny=["write_file", "未闭合(", "", 7, "x y(", "a)b"])
        # 只留下合法的裸 write_file；坏条目不炸、不进策略
        assert p.denies_tool("write_file", "anything")
        assert not p.denies_tool("read_file", "anything")
        assert not p.denies_tool("run_command", "anything")


# -- 别名映射 -------------------------------------------------------------
class TestAliases:
    def test_bash_maps_to_run_command(self) -> None:
        p = _pol(tool_deny=["Bash(pip install *)"])
        assert p.denies_tool("run_command", "pip install numpy")

    def test_engine_name_also_accepted(self) -> None:
        p = _pol(tool_deny=["run_command(rm -rf build)"])
        assert p.denies_tool("run_command", "rm -rf build")

    def test_write_edit_group_covers_both_writers(self) -> None:
        p = _pol(tool_deny=["Write(secret.txt)"])
        assert p.denies_tool("write_file", "secret.txt")
        assert p.denies_tool("file_edit", "secret.txt")
        # Edit 规则同样拦 write_file
        p2 = _pol(tool_deny=["Edit(secret.txt)"])
        assert p2.denies_tool("write_file", "secret.txt")

    def test_write_group_does_not_match_read(self) -> None:
        p = _pol(tool_deny=["Write(secret.txt)"])
        assert not p.denies_tool("read_file", "secret.txt")

    def test_unknown_tool_only_self(self) -> None:
        p = _pol(tool_deny=["mcp__srv__do(x)"])
        assert p.denies_tool("mcp__srv__do", "x")
        assert not p.denies_tool("read_file", "x")


# -- 匹配语义 -------------------------------------------------------------
class TestMatching:
    def test_wildcard(self) -> None:
        p = _pol(tool_allow=["Write(src/*)"])
        assert p.allows_tool("write_file", "src/a.py")
        assert not p.allows_tool("write_file", "other/a.py")

    def test_prefix_rule_semantics(self) -> None:
        p = _pol(tool_allow=["Bash(python -m pytest)"])
        # 前缀规则：更长命令也命中（claude 语义）
        assert p.allows_tool("run_command", "python -m pytest tests/unit")
        assert p.allows_tool("run_command", "python -m pytest")
        assert not p.allows_tool("run_command", "python -m other")

    def test_path_spec_is_workspace_relative(self, tmp_path: Path) -> None:
        p = _pol(ws=tmp_path, tool_deny=["Write(.env)"])
        # spec 是"工作区相对路径"：根目录 .env 命中，子目录 sub/.env 不命中
        # （pattern 无通配时是前缀规则，".env" 不是 "sub/.env" 的前缀）
        assert p.denies_tool("write_file", ".env")
        assert not p.denies_tool("write_file", "sub/.env")
        # 通配可写任意层级：Write(**/.env) 命中子目录
        p2 = _pol(ws=tmp_path, tool_deny=["Write(**/.env)"])
        assert p2.denies_tool("write_file", "sub/.env")
        # 工作区外的路径 spec=None：路径工具规则只对工作区内生效
        assert not p.denies_tool("write_file", None)


# -- gate 集成：deny / allow 裁决 ------------------------------------------
class TestGateEnforcement:
    def test_deny_wins_in_full_and_over_confirm(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_deny=["Bash(rm *)"])
        g = _set_tool(PermissionGate(PermissionMode.FULL, ws, policy=pol), "run_command")
        with pytest.raises(Denied):
            g.authorize_command("rm -rf build", confirm=lambda _: True)  # full+confirm 也救不回
        # 不在规则里的命令 full 放行（证明不是一刀切）
        g.authorize_command("python -m pytest")

    def test_deny_overrides_allow(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_deny=["Bash(rm *)"], tool_allow=["Bash(rm *)"])
        g = _set_tool(PermissionGate(PermissionMode.AUTO, ws, policy=pol), "run_command")
        with pytest.raises(Denied):
            g.authorize_command("rm -rf build")

    def test_allow_shortcircuits_ask_for_command(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_allow=["Bash(rm -rf build)"])
        g = _set_tool(PermissionGate(PermissionMode.AUTO, ws, policy=pol), "run_command")
        g.authorize_command("rm -rf build")  # 危险命令本应询问，allow 命中直接放行
        g2 = _set_tool(PermissionGate(PermissionMode.AUTO, ws), "run_command")
        with pytest.raises(Denied):
            g2.authorize_command("rm -rf build")  # 无 allow -> 无 confirm 拒

    def test_allow_shortcircuits_ask_for_write(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_allow=["Write(src/**)"])
        g = _set_tool(PermissionGate(PermissionMode.ASK, ws, policy=pol), "write_file")
        g.authorize_write(ws / "src" / "a.py")  # ASK 模式命中 allow -> 免问
        with pytest.raises(Denied):
            g.authorize_write(ws / "other" / "b.py")  # 不在 allow -> 无 confirm 拒

    def test_allow_never_exempts_sensitive_write(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_allow=["Write(.env)"])
        g = _set_tool(PermissionGate(PermissionMode.ASK, ws, policy=pol), "write_file")
        with pytest.raises(Denied):
            g.authorize_write(ws / ".env")  # 敏感写仍需人工批准，allow 不豁免
        g.authorize_write(ws / ".env", confirm=lambda _: True)  # 人工批准仍可

    def test_allow_never_exempts_hard_block(self, tmp_path: Path) -> None:
        ws = tmp_path
        pol = _pol(ws=ws, tool_allow=["Bash(rm -rf /)"])
        g = _set_tool(PermissionGate(PermissionMode.FULL, ws, policy=pol), "run_command")
        with pytest.raises(Denied):
            g.authorize_command("rm -rf /")  # 硬拦截无条件，allow 救不回

    def test_read_denied_by_tool_rule(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / ".env").write_text("K=1", encoding="utf-8")
        pol = _pol(ws=ws, tool_deny=["Read(.env)"])
        g = _set_tool(PermissionGate(PermissionMode.FULL, ws, policy=pol), "read_file")
        with pytest.raises(Denied):
            g.authorize_read(ws / ".env")
        g.authorize_read(ws / "app.py")  # 不在规则 -> 放行

    def test_external_deny_and_allow(self, tmp_path: Path) -> None:
        ws = tmp_path
        deny_pol = _pol(ws=ws, tool_deny=["web_search"])
        g = _set_tool(PermissionGate(PermissionMode.FULL, ws, policy=deny_pol), "web_search")
        with pytest.raises(Denied):
            g.authorize_external("web_search")
        allow_pol = _pol(ws=ws, tool_allow=["WebSearch"])
        g2 = _set_tool(PermissionGate(PermissionMode.ASK, ws, policy=allow_pol), "web_search")
        g2.authorize_external("web_search")  # ASK 命中 allow -> 免问
        with pytest.raises(Denied):
            _set_tool(PermissionGate(PermissionMode.ASK, ws), "web_search").authorize_external("web_search")


# -- 装配：load_permission_policy + env 门控 ---------------------------------
class TestLoader:
    SETTINGS = {
        "permissions": {
            "allow": ["Bash(git status)", "Read(src/**)"],
            "deny": ["run_command(pip install *)", "Write(.env)"],
        }
    }

    def _write_settings(self, ws: Path, content: object) -> None:
        cfg = ws / ".claude" / "settings.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(content), encoding="utf-8")

    def test_gated_by_env(self, tmp_path: Path, monkeypatch) -> None:
        self._write_settings(tmp_path, self.SETTINGS)
        monkeypatch.delenv("EMBERPY_PROJECT_SETTINGS", raising=False)
        monkeypatch.delenv("EMBERPY_DENY", raising=False)
        monkeypatch.delenv("EMBERPY_PROJECT_PERMISSIONS", raising=False)
        p_off = load_permission_policy(tmp_path)
        assert not p_off.allows_tool("run_command", "git status")
        assert not p_off.denies_tool("run_command", "pip install numpy")
        monkeypatch.setenv("EMBERPY_PROJECT_SETTINGS", "1")
        p_on = load_permission_policy(tmp_path)
        assert p_on.allows_tool("run_command", "git status")
        assert p_on.denies_tool("run_command", "pip install numpy")
        assert p_on.denies_tool("write_file", ".env")

    def test_missing_and_bad_json_silent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EMBERPY_PROJECT_SETTINGS", "1")
        monkeypatch.delenv("EMBERPY_DENY", raising=False)
        monkeypatch.delenv("EMBERPY_PROJECT_PERMISSIONS", raising=False)
        assert not load_permission_policy(tmp_path).allows_tool("run_command", "x")  # 无文件
        self._write_settings(tmp_path, "{ 坏 json")
        assert not load_permission_policy(tmp_path).denies_tool("run_command", "x")  # 坏 JSON

    def test_coexists_with_path_deny_env(self, tmp_path: Path, monkeypatch) -> None:
        self._write_settings(tmp_path, self.SETTINGS)
        monkeypatch.setenv("EMBERPY_PROJECT_SETTINGS", "1")
        monkeypatch.setenv("EMBERPY_DENY", json.dumps(["/secrets.json"]))
        p = load_permission_policy(tmp_path)
        assert p.denies(tmp_path / "secrets.json")  # 路径 deny（env）
        assert p.denies_tool("run_command", "pip install x")  # 工具 deny（settings）
        assert p.allows_tool("run_command", "git status")  # allow（settings）
