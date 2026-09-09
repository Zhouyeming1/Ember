"""权限门按模式裁决：写文件 / 命令 / 读文件。"""

from pathlib import Path

import pytest

from emberpy.errors import Denied
from emberpy.permission import PermissionGate, PermissionMode


# -- 写文件决策 ----------------------------------------------------------
class TestWrite:
    def test_plan_denies_write_anywhere(self) -> None:
        g = PermissionGate(PermissionMode.PLAN, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_write(Path("/ws/a.txt"))

    def test_auto_allows_inside(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        g.authorize_write(Path("/ws/sub/a.txt"))  # 不抛即通过

    def test_auto_asks_outside_no_confirm_denies(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_write(Path("/etc/evil.txt"))

    def test_auto_outside_confirmed(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        g.authorize_write(Path("/etc/evil.txt"), confirm=lambda _: True)

    def test_ask_needs_confirm(self) -> None:
        g = PermissionGate(PermissionMode.ASK, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_write(Path("/ws/a.txt"))  # 无 confirm -> fail closed
        with pytest.raises(Denied):
            g.authorize_write(Path("/ws/a.txt"), confirm=lambda _: False)  # 用户拒绝
        g.authorize_write(Path("/ws/a.txt"), confirm=lambda _: True)  # 用户批准

    def test_full_allows_outside(self) -> None:
        g = PermissionGate(PermissionMode.FULL, Path("/ws"))
        g.authorize_write(Path("/etc/evil.txt"))


# -- 命令决策 ------------------------------------------------------------
class TestCommands:
    def test_auto_dangerous_needs_confirm(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_command("rm -rf build")
        g.authorize_command("rm -rf build", confirm=lambda _: True)

    def test_auto_safe_passes(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        g.authorize_command("python -m pytest")

    def test_plan_only_read_only(self) -> None:
        g = PermissionGate(PermissionMode.PLAN, Path("/ws"))
        g.authorize_command("git status")
        with pytest.raises(Denied):
            g.authorize_command("python -m pytest")

    def test_blocked_even_in_full(self) -> None:
        g = PermissionGate(PermissionMode.FULL, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_command("rm -rf /")

    def test_ask_readonly_passes_silently(self) -> None:
        g = PermissionGate(PermissionMode.ASK, Path("/ws"))
        g.authorize_command("git log")  # 只读诊断命令在 ask 下静默放行


# -- 读文件决策 ----------------------------------------------------------
class TestReads:
    def test_read_inside_all_modes(self) -> None:
        for mode in PermissionMode:
            g = PermissionGate(mode, Path("/ws"))
            g.authorize_read(Path("/ws/src/a.py"))

    def test_read_outside_denied_in_plan(self) -> None:
        g = PermissionGate(PermissionMode.PLAN, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_read(Path("/etc/passwd"))

    def test_read_outside_denied_auto_without_confirm(self) -> None:
        g = PermissionGate(PermissionMode.AUTO, Path("/ws"))
        with pytest.raises(Denied):
            g.authorize_read(Path("/etc/passwd"))
