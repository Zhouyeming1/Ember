"""文件系统/命令工具测试（走真实 gate + PatchStore，tmp 工作区）。"""

from pathlib import Path

import pytest

from emberpy.errors import Denied
from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.tools import ToolEnv, default_registry


def _env(workspace: Path, mode: str = "auto", confirm=None) -> tuple[object, PermissionGate, PatchStore]:
    gate = PermissionGate(PermissionMode(mode), workspace)
    patches = PatchStore()
    env = ToolEnv(workspace=workspace, gate=gate, patches=patches, confirm=confirm)
    return default_registry(env), gate, patches


class TestReadTools:
    def test_read_file(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("read_file").fn(path="hello.txt")
        assert "你好" in out

    def test_read_missing_file(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        assert "不存在" in reg.get("read_file").fn(path="nope.txt")

    def test_list_dir(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("list_dir").fn(path=".")
        assert "hello.txt" in out
        assert "src" in out

    def test_grep(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("grep").fn(pattern="def greet", path=".")
        assert "app.py:1" in out

    def test_read_escape_outside(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        assert "越界" in reg.get("read_file").fn(path="../secret.txt")


class TestWriteTools:
    def test_write_inside_and_undo(self, workspace: Path) -> None:
        reg, _, patches = _env(workspace)
        out = reg.get("write_file").fn(path="note.txt", content="hi")
        assert "已写入" in out
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "hi"
        assert len(patches) == 1
        patches.undo_last()
        assert not (workspace / "note.txt").exists()

    def test_write_outside_denied(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("write_file").fn(path="../hack.txt", content="x")
        assert "越界" in out or "拒绝" in out

    def test_write_absolute_inside_ok(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        target = workspace / "abs.txt"
        out = reg.get("write_file").fn(path=str(target), content="ok")
        assert "已写入" in out
        assert target.read_text(encoding="utf-8") == "ok"


class TestFileEdit:
    def test_edit_inside_and_undo(self, workspace: Path) -> None:
        reg, _, patches = _env(workspace)
        # 严格写前必读：编辑已存在文件前先 read_file
        reg.get("read_file").fn(path="src/app.py")
        out = reg.get("file_edit").fn(path="src/app.py", old_string="def greet", new_string="def hello")
        assert "已修改" in out
        text = (workspace / "src" / "app.py").read_text(encoding="utf-8")
        assert "def hello():" in text
        assert "def greet():" not in text
        assert len(patches) == 1
        patches.undo_last()
        restored = (workspace / "src" / "app.py").read_text(encoding="utf-8")
        assert "def greet():" in restored

    def test_edit_missing_file_hints_write_file(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("file_edit").fn(path="nope.py", old_string="a", new_string="b")
        assert "write_file" in out

    def test_edit_not_found_hints_read(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        reg.get("read_file").fn(path="src/app.py")
        out = reg.get("file_edit").fn(path="src/app.py", old_string="def missing", new_string="def x")
        assert "read_file" in out

    def test_edit_without_read_denied(self, workspace: Path) -> None:
        """严格写前必读：没 read_file 过已有文件就 edit -> 拒绝并引导先读。"""
        reg, _, _ = _env(workspace)
        out = reg.get("file_edit").fn(path="src/app.py", old_string="def greet", new_string="def hello")
        assert "read_file" in out
        assert "已修改" not in out
        # 文件未被改动
        text = (workspace / "src" / "app.py").read_text(encoding="utf-8")
        assert "def greet():" in text

    def test_edit_outside_denied(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("file_edit").fn(path="../secret.py", old_string="a", new_string="b")
        assert "越界" in out or "拒绝" in out

    def test_edit_plan_mode_denied(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace, mode="plan")
        # R-G③：Denied 不再被吞成文本，直接向 agent._execute 传播
        with pytest.raises(Denied):
            reg.get("file_edit").fn(path="src/app.py", old_string="def greet", new_string="def hello")


class TestWriteBeforeRead:
    """P1 严格写前必读护栏：覆盖既有文件要"先读、读后文件没被改过"。"""

    def test_overwrite_existing_without_read_denied(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("write_file").fn(path="src/app.py", content="x = 1")
        assert "先 read_file" in out
        assert (workspace / "src" / "app.py").read_text(encoding="utf-8") != "x = 1"

    def test_overwrite_existing_after_read_ok(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        reg.get("read_file").fn(path="src/app.py")
        out = reg.get("write_file").fn(path="src/app.py", content="x = 1\n")
        assert "已写入" in out
        assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_write_stale_after_external_change_denied(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        reg.get("read_file").fn(path="src/app.py")
        # 外部（本测试）改了文件，指纹变了
        (workspace / "src" / "app.py").write_text(
            "def other():\n    pass\n", encoding="utf-8", newline="\n"
        )
        out = reg.get("file_edit").fn(
            path="src/app.py", old_string="def greet", new_string="def hello"
        )
        assert "外部改动" in out
        assert "已修改" not in out

    def test_new_file_no_read_needed(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("write_file").fn(path="brand_new.txt", content="hello\n")
        assert "已写入" in out


class TestReadNewTools:
    """P1 新能力：glob / read 分页 / 重复读去重 / grep 输出模式。"""

    def test_glob(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("glob").fn(pattern="**/*.py")
        assert "src/app.py" in out
        assert "hello.txt" not in out

    def test_glob_bad_pattern(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        assert "不能为空" in reg.get("glob").fn(pattern="")

    def test_read_pagination(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        first = reg.get("read_file").fn(path="src/app.py", offset=0, limit=2)
        assert "1: def greet():" in first
        assert "3:" not in first
        second = reg.get("read_file").fn(path="src/app.py", offset=2, limit=2)
        assert "3:" in second
        assert "4:" in second

    def test_read_dedupe_stub_then_stale_content(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        reg.get("read_file").fn(path="hello.txt")
        again = reg.get("read_file").fn(path="hello.txt")
        assert "没有变化" in again
        # 外部改动后再读 -> 回到正常内容
        (workspace / "hello.txt").write_text("改了\n", encoding="utf-8", newline="\n")
        changed = reg.get("read_file").fn(path="hello.txt")
        assert "改了" in changed
        assert "没有变化" not in changed

    def test_grep_files_mode(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("grep").fn(pattern="def", path=".", output_mode="files")
        assert "src/app.py" in out
        assert ":" not in out.splitlines()[0] or "src/app.py" == out.splitlines()[0]

    def test_grep_count_mode(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("grep").fn(pattern="def", path=".", output_mode="count")
        assert "src/app.py: 2" in out

    def test_grep_bad_output_mode(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("grep").fn(pattern="def", path=".", output_mode="bogus")
        assert "output_mode" in out


class TestShellTool:
    def test_safe_command(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        out = reg.get("run_command").fn(command="echo hello-emberpy")
        assert out.strip().startswith("hello-emberpy")

    def test_dangerous_denied_without_confirm(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace)
        # R-G③：Denied 传播（agent 层统一转工具结果 + PermissionDenied hook）
        with pytest.raises(Denied):
            reg.get("run_command").fn(command="rm -rf build")

    def test_dangerous_allowed_after_confirm(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace, confirm=lambda _: True)
        out = reg.get("run_command").fn(command="echo rm-safe")
        assert "拒绝" not in out

    def test_plan_denies_non_readonly(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace, mode="plan")
        with pytest.raises(Denied):
            reg.get("run_command").fn(command="python -m pytest")

    def test_hard_blocked_even_full(self, workspace: Path) -> None:
        reg, _, _ = _env(workspace, mode="full")
        with pytest.raises(Denied):
            reg.get("run_command").fn(command="rm -rf /")
