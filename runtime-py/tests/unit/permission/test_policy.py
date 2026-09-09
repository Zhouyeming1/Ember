"""Wave B（R-B/R-C）：deny 规则 + 敏感集合 + symlink 双查的权限策略测试。

deny 语义对齐 claude：无条件拒绝（压过 confirm、压过任何权限模式含 full）。
敏感集合语义：.git/.bashrc/.env 等在 full/auto 下也不能静默写——需一次人工批准，
无交互（confirm=None）即 fail closed（对齐 claude DANGEROUS 文件/目录）。
读文件只受 deny 约束（读 .env 这类仍放行），不受敏感集合约束。
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
from emberpy.testing import make_agent, tool_call


def _tool_texts(agent) -> str:
    """把一次 agent 跑完的会话里所有 tool 结果拼起来（断言权限拒绝文本用）。"""
    out: list[str] = []
    for msg in agent.session.messages():
        if isinstance(msg, dict) and msg.get("role") == "tool":
            out.append(str(msg.get("content", "")))
    return "\n".join(out)


# -- deny（无条件） ------------------------------------------------------
class TestDeny:
    def test_deny_unconditional_even_in_full_overrides_confirm(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / ".git").mkdir()
        policy = PermissionPolicy(ws, deny_patterns=["/config.yml"])
        g = PermissionGate(PermissionMode.FULL, ws, policy=policy)
        # 命中 deny 的 confirm=True 也救不回（对齐 claude "deny 免疫一切"）
        with pytest.raises(Denied):
            g.authorize_write(ws / "config.yml", confirm=lambda _: True)
        # 不在 deny 名单、也不是敏感/越界 -> full 放行（证明不是一刀切）
        g.authorize_write(ws / "config.json")

    def test_deny_covers_dotgit_and_bashrc(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / ".git").mkdir()
        policy = PermissionPolicy(ws, deny_patterns=[".git", ".bashrc"])
        g = PermissionGate(PermissionMode.AUTO, ws, policy=policy)
        with pytest.raises(Denied):
            g.authorize_write(ws / ".git" / "config")
        with pytest.raises(Denied):
            g.authorize_write(ws / ".bashrc")

    def test_deny_applies_to_reads(self, tmp_path: Path) -> None:
        ws = tmp_path
        policy = PermissionPolicy(ws, deny_patterns=["secrets.json"])
        g = PermissionGate(PermissionMode.FULL, ws, policy=policy)
        with pytest.raises(Denied):
            g.authorize_read(ws / "secrets.json")
        g.authorize_read(ws / "app.py")  # 非 deny 路径正常

    def test_load_policy_from_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EMBERPY_DENY", json.dumps([".env", "/build/"]))
        policy = load_permission_policy(tmp_path)
        assert policy.denies(tmp_path / ".env")
        assert policy.denies(tmp_path / "sub" / ".env")  # 未锚定对任意层级同名
        assert policy.denies(tmp_path / "build")
        assert not policy.denies(tmp_path / "src" / "x.txt")

    def test_project_permissions_gated_by_env(self, tmp_path: Path, monkeypatch) -> None:
        proj = tmp_path / ".ember"
        proj.mkdir(parents=True)
        (proj / "permissions.json").write_text(
            '{"permissions": {"deny": ["/settings.json"]}}', encoding="utf-8"
        )
        monkeypatch.delenv("EMBERPY_PROJECT_PERMISSIONS", raising=False)
        monkeypatch.delenv("EMBERPY_DENY", raising=False)
        assert not load_permission_policy(tmp_path).denies(tmp_path / "settings.json")
        monkeypatch.setenv("EMBERPY_PROJECT_PERMISSIONS", "1")
        assert load_permission_policy(tmp_path).denies(tmp_path / "settings.json")

    def test_bad_deny_json_ignored(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EMBERPY_DENY", "{ 不是数组")
        # 坏 env JSON 静默 -> 空 deny，不炸 worker
        policy = load_permission_policy(tmp_path)
        assert not policy.denies(tmp_path / "anything.txt")


# -- 敏感集合（full/auto 下也要人工批准） ---------------------------------
class TestSensitive:
    def test_full_denies_git_write_without_confirm(self, tmp_path: Path) -> None:
        ws = tmp_path
        g = PermissionGate(PermissionMode.FULL, ws)  # 默认策略（deny 空、敏感常开）
        with pytest.raises(Denied):
            g.authorize_write(ws / ".git" / "config")
        g.authorize_write(ws / ".git" / "config", confirm=lambda _: True)  # 人工批准后放行

    def test_auto_denies_bashrc_within_workspace_without_confirm(self, tmp_path: Path) -> None:
        ws = tmp_path
        g = PermissionGate(PermissionMode.AUTO, ws)
        # .bashrc 在工作区内、auto 常规放行区，但命中敏感集合 -> 仍要批准
        with pytest.raises(Denied):
            g.authorize_write(ws / ".bashrc")
        g.authorize_write(ws / ".bashrc", confirm=lambda _: True)

    def test_env_write_requires_approval_but_read_free(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / ".env").write_text("K=1", encoding="utf-8")
        g = PermissionGate(PermissionMode.AUTO, ws)
        with pytest.raises(Denied):
            g.authorize_write(ws / ".env")
        # 读 .env 不受敏感集合约束（deny 空 -> 放行）——agent 需要读本地凭据来跑 app
        g.authorize_read(ws / ".env")
        # .env.example 不算敏感（脚手架常写），auto 工作区内放行
        g.authorize_write(ws / ".env.example")

    def test_sensitive_through_symlink_canonical(self, tmp_path: Path) -> None:
        """symlink 指进工作区内 .git：canonical 解析后仍是敏感路径 -> 拦。"""
        ws = tmp_path
        git_dir = ws / ".git"
        git_dir.mkdir()
        link = ws / "link"
        try:
            link.symlink_to(git_dir, target_is_directory=True)
        except OSError:
            pytest.skip("当前平台无法创建符号链接")
        g = PermissionGate(PermissionMode.FULL, ws)
        with pytest.raises(Denied):
            g.authorize_write(git_dir / "config", lexical=link / "config")

    def test_sensitive_through_symlink_lexical(self, tmp_path: Path) -> None:
        """词法路径含敏感段但 canonical 不含：双查靠 lexical 兜住。

        .env 是个 symlink 指向普通文件 plain.txt。canonical 是 plain.txt（auto
        工作区内会放行），但用户词法上是写 .env —— 双查在 lexical 上命中敏感。
        """
        ws = tmp_path
        (ws / "plain.txt").write_text("x", encoding="utf-8")
        try:
            (ws / ".env").symlink_to(ws / "plain.txt")
        except OSError:
            pytest.skip("当前平台无法创建符号链接")
        g = PermissionGate(PermissionMode.AUTO, ws)
        canonical = (ws / "plain.txt").resolve()
        lexical = ws / ".env"
        # 单查 canonical 会放行（工作区内普通文件）；词法 .env 命中敏感 -> 拦
        with pytest.raises(Denied):
            g.authorize_write(canonical, lexical=lexical)
        # 人工批准则放行
        g.authorize_write(canonical, lexical=lexical, confirm=lambda _: True)


# -- agentic 端到端：写敏感/deny 目标被拒并回文本给模型 -------------------
class TestAgenticDeny:
    def test_full_write_git_config_refused(self, workspace: Path) -> None:
        (workspace / ".git").mkdir(exist_ok=True)
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("w1", "write_file", {"path": ".git/config", "content": "[core]"})]},
                {"content": "我放弃了写它", "calls": None},
            ],
            workspace,
            mode="full",
        )
        agent.run("往 .git/config 写东西")
        text = _tool_texts(agent)
        assert "被拒绝" in text
        # 没有真正写进去（gate 在 apply 之前拦截）
        assert not (workspace / ".git" / "config").exists()

    def test_auto_write_env_refused(self, workspace: Path) -> None:
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("w1", "write_file", {"path": ".env", "content": "K=1"})]},
                {"content": "被拦了", "calls": None},
            ],
            workspace,
            mode="auto",
        )
        agent.run("创建 .env")
        text = _tool_texts(agent)
        assert "被拒绝" in text
        assert not (workspace / ".env").exists()
