"""权限域纯逻辑：命令分类 + 路径作用域（不涉及具体模式裁决）。"""

from pathlib import Path

import pytest

from emberpy.permission import CommandKind, classify_command, is_within, resolve_within


# -- 命令分类 ------------------------------------------------------------
class TestClassifyCommand:
    def test_hard_block_root_delete(self) -> None:
        assert classify_command("rm -rf /") is CommandKind.BLOCKED
        assert classify_command("rm -rf /tmp/* /") is CommandKind.BLOCKED

    def test_hard_block_block_device(self) -> None:
        assert classify_command("dd if=/dev/zero of=/dev/sda") is CommandKind.BLOCKED

    def test_hard_block_fork_bomb(self) -> None:
        assert classify_command(":(){ :|:& };:") is CommandKind.BLOCKED

    def test_dangerous_rm_rf(self) -> None:
        assert classify_command("rm -rf build") is CommandKind.DANGEROUS

    def test_dangerous_force_push(self) -> None:
        assert classify_command("git push --force origin main") is CommandKind.DANGEROUS

    def test_read_only_git(self) -> None:
        assert classify_command("git status") is CommandKind.READ_ONLY

    def test_safe_plain(self) -> None:
        assert classify_command("python -m pytest") is CommandKind.SAFE
        assert classify_command("echo hello") is CommandKind.READ_ONLY

    # -- R-D：workspace 感知的目标扫描 --------------------------------------
    def test_redirect_into_system_is_dangerous(self, tmp_path: Path) -> None:
        # echo 在白名单里本来算只读；重定向落到系统目录必须升危险
        assert classify_command("echo x > /etc/cron.d/e", tmp_path) is CommandKind.DANGEROUS

    def test_redirect_outside_workspace_is_dangerous(self, tmp_path: Path) -> None:
        assert classify_command("echo hi > /tmp/out.txt", tmp_path) is CommandKind.DANGEROUS
        # /dev/null 这类无害 sink 不升危险（echo > /dev/null 保持只读）
        assert classify_command("echo hi > /dev/null", tmp_path) is CommandKind.READ_ONLY

    def test_redirect_inside_workspace_not_read_only(self, tmp_path: Path) -> None:
        # 工作区内写文件：不再是"只读诊断"（plan 不能放行），但也不算危险（auto 放行）
        assert classify_command("echo hi > note.txt", tmp_path) is CommandKind.SAFE
        assert classify_command("ls > listing.txt", tmp_path) is CommandKind.SAFE

    def test_rm_system_file_without_rf_is_dangerous(self, tmp_path: Path) -> None:
        # 无 -r/-f 的 rm /etc/passwd 以前漏网当 safe；目标扫描兜住
        assert classify_command("rm /etc/passwd", tmp_path) is CommandKind.DANGEROUS
        assert classify_command("rm -rf /etc/ssh", tmp_path) is CommandKind.DANGEROUS

    def test_rm_inside_workspace_stays_safe(self, tmp_path: Path) -> None:
        assert classify_command("rm old.log", tmp_path) is CommandKind.SAFE

    def test_sed_inplace_on_system_is_dangerous(self, tmp_path: Path) -> None:
        assert classify_command("sed -i s/x/y/ /etc/hosts", tmp_path) is CommandKind.DANGEROUS
        # 非就地 sed（只读到 stdout）写不到文件，不当危险
        assert classify_command("sed s/x/y/ file.txt", tmp_path) is CommandKind.SAFE

    def test_write_to_env_is_dangerous(self, tmp_path: Path) -> None:
        assert classify_command("echo K=1 > .env", tmp_path) is CommandKind.DANGEROUS
        assert classify_command("touch .env", tmp_path) is CommandKind.DANGEROUS

    def test_ifs_variable_bypass_is_dangerous(self, tmp_path: Path) -> None:
        # 把命令字拆成 $IFS 连接，绕过 "\bcmd\b + 空格" 正则
        assert classify_command("c$IFSat /etc/passwd", tmp_path) is CommandKind.DANGEROUS
        assert classify_command("echo x ${IFS}&& id", tmp_path) is CommandKind.DANGEROUS

    def test_no_workspace_keeps_workspace_independent_dims(self) -> None:
        # 不给 workspace（旧调用/纯字符串）：越界维度退化为不判，但系统前缀/敏感名
        # 这些"不需要 workspace 也成立"的维度仍在 -> 危险目标仍能升 DANGEROUS
        assert classify_command("echo hi > /etc/x") is CommandKind.DANGEROUS
        assert classify_command("rm /etc/passwd") is CommandKind.DANGEROUS
        assert classify_command("echo hi") is CommandKind.READ_ONLY


# -- 路径作用域 ----------------------------------------------------------
class TestPaths:
    def test_is_within(self) -> None:
        assert is_within(Path("/a/b/c.txt"), Path("/a"))
        assert not is_within(Path("/a/../b.txt"), Path("/a"))

    def test_resolve_relative(self, tmp_path: Path) -> None:
        got = resolve_within(tmp_path, "sub/f.txt")
        assert got == (tmp_path / "sub/f.txt").resolve()

    def test_resolve_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "../outside")

    def test_resolve_absolute_outside_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "/etc/passwd")

    def test_resolve_symlink_escape(self, tmp_path: Path) -> None:
        inside = tmp_path / "in"
        inside.mkdir()
        link = tmp_path / "link"
        outside = tmp_path / "out"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("当前平台无法创建符号链接")
        with pytest.raises(ValueError):
            resolve_within(inside, "link/secret.txt")
