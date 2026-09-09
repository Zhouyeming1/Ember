"""补丁 / checkpoint / undo 测试。"""

from pathlib import Path

import pytest

from emberpy.patches import FileToolError, NotTextFileError, PatchStore


@pytest.fixture()
def store(tmp_path: Path) -> PatchStore:
    return PatchStore()


class TestApplyWrite:
    def test_create_new_file(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"
        patch = store.apply_write(target, "第一行\n第二行\n")
        assert patch.before is None
        assert target.read_text(encoding="utf-8") == "第一行\n第二行\n"
        assert len(store) == 1

    def test_overwrite_records_before(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("旧内容\n", encoding="utf-8", newline="\n")
        patch = store.apply_write(target, "新内容\n")
        assert patch.before == "旧内容\n"
        assert target.read_text(encoding="utf-8") == "新内容\n"

    def test_rejects_nul_bytes(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "bin.dat"
        with pytest.raises(NotTextFileError):
            store.apply_write(target, "abc\x00def")

    def test_rejects_binary_overwrite(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "img.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
        with pytest.raises(NotTextFileError):
            store.apply_write(target, "伪装成文本的整写")


class TestUndo:
    def test_undo_restores_overwritten(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("旧\n", encoding="utf-8", newline="\n")
        store.apply_write(target, "新\n")
        patch = store.undo_last()
        assert patch is not None
        assert patch.before == "旧\n"
        assert target.read_text(encoding="utf-8") == "旧\n"

    def test_undo_removes_created_file(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"
        store.apply_write(target, "x\n")
        store.undo_last()
        assert not target.exists()

    def test_undo_last_only_one_level(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        store.apply_write(target, "v1\n")
        store.apply_write(target, "v2\n")
        store.undo_last()
        assert target.read_text(encoding="utf-8") == "v1\n"
        assert len(store) == 1
        store.undo_last()
        assert not target.exists()
        assert store.undo_last() is None  # 无可回滚

    def test_undo_path(self, store: PatchStore, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        store.apply_write(a, "a1\n")
        store.apply_write(b, "b1\n")
        store.apply_write(a, "a2\n")
        patch = store.undo_path(a)
        assert patch is not None and patch.before == "a1\n"
        assert a.read_text(encoding="utf-8") == "a1\n"
        assert b.read_text(encoding="utf-8") == "b1\n"


class TestApplyEdit:
    def _seed(self, tmp_path: Path, content: str) -> Path:
        target = tmp_path / "code.py"
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

    def test_single_replacement_records_full_before(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "def greet():\n    return 'hi'\n")
        patch = store.apply_edit(target, "def greet", "def hello")
        assert patch.before == "def greet():\n    return 'hi'\n"
        assert patch.after == "def hello():\n    return 'hi'\n"
        assert target.read_text(encoding="utf-8") == "def hello():\n    return 'hi'\n"
        assert len(store) == 1

    def test_replace_all_replaces_every_occurrence(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "print(greet())\nprint(greet())\n")
        store.apply_edit(target, "greet", "hello", replace_all=True)
        assert target.read_text(encoding="utf-8") == "print(hello())\nprint(hello())\n"

    def test_not_found_raises_and_leaves_file_unchanged(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "def greet():\n    return 'hi'\n")
        with pytest.raises(FileToolError, match="找不到"):
            store.apply_edit(target, "def nope", "def x")
        assert target.read_text(encoding="utf-8") == "def greet():\n    return 'hi'\n"
        assert len(store) == 0  # 失败不留假 checkpoint

    def test_ambiguous_without_replace_all_raises(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "a\nb\na\n")
        with pytest.raises(FileToolError, match="2 次"):
            store.apply_edit(target, "a", "x")
        store.apply_edit(target, "a", "x", replace_all=True)
        assert target.read_text(encoding="utf-8") == "x\nb\nx\n"

    def test_same_old_new_raises(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "hello\n")
        with pytest.raises(FileToolError, match="相同"):
            store.apply_edit(target, "hello", "hello")

    def test_empty_old_string_raises(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "hello\n")
        with pytest.raises(FileToolError, match="不能为空"):
            store.apply_edit(target, "", "x")

    def test_missing_file_raises(self, store: PatchStore, tmp_path: Path) -> None:
        with pytest.raises(FileToolError, match="write_file"):
            store.apply_edit(tmp_path / "no.py", "a", "b")

    def test_undo_restores_original(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "def greet():\n    return 'hi'\n")
        store.apply_edit(target, "greet", "hello")
        patch = store.undo_last()
        assert patch is not None and patch.before == "def greet():\n    return 'hi'\n"
        assert target.read_text(encoding="utf-8") == "def greet():\n    return 'hi'\n"

    def test_seq_monotonic_across_edits(self, store: PatchStore, tmp_path: Path) -> None:
        target = self._seed(tmp_path, "v1\nv2\n")
        p1 = store.apply_edit(target, "v1", "x1")
        p2 = store.apply_edit(target, "v2", "x2")
        assert p2.seq == p1.seq + 1


class TestDelete:
    def test_delete_then_undo_restores(self, store: PatchStore, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("内容\n", encoding="utf-8", newline="\n")
        patch = store.apply_delete(target)
        assert not target.exists()
        store.undo_last()
        assert target.read_text(encoding="utf-8") == "内容\n"
        assert patch.after is None

    def test_delete_missing_file_fails(self, store: PatchStore, tmp_path: Path) -> None:
        with pytest.raises(FileToolError):
            store.apply_delete(tmp_path / "不存在.txt")
