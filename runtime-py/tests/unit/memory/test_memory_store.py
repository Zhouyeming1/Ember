"""memory/memdir 文件模型单元测试。

覆盖：
- save 建目录 + 写 frontmatter 文件，read 往返一致；
- 覆盖同名 = 更新（不产生重复索引行）；
- MEMORY.md 纯索引：格式 ``- [Title](name.md) — hook``，无 frontmatter；
- list 最新在前、跳过 MEMORY.md、目录不存在返回空且**不建目录**；
- 名字/类型校验：拒绝穿越、坏字符、非闭集类型；
- index_text 超行数/字节数截断并附 WARNING；
- resolve_memory_dir：override 优先，EMBER_HOME 布局 <home>/projects/<slug>/memory；
- build_memory_prompt 空索引提示 / 非空带索引。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from emberpy.errors import FileToolError
from emberpy.memory import MemoryStore, build_memory_prompt, resolve_memory_dir


def _store(root: Path) -> MemoryStore:
    return MemoryStore(root)


# ---------------------------------------------------------------------------
# save / read 往返
# ---------------------------------------------------------------------------


def test_save_creates_file_with_frontmatter_and_index(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    result = store.save("user_preferences", "回复要简洁", "feedback", "用户要求简体中文、代码用英文。")

    assert result.created is True
    path = tmp_path / "mem" / "user_preferences.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: user_preferences\n")
    assert "type: feedback" in text
    assert "用户要求简体中文" in text

    index = (tmp_path / "mem" / "MEMORY.md").read_text(encoding="utf-8")
    assert index == "- [user_preferences](user_preferences.md) — 回复要简洁\n"
    assert not index.startswith("---")  # 索引不是 frontmatter 文件


def test_read_roundtrip_and_missing(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    store.save("proj", "冻结期", "project", "2026-09-15 起冻结非关键合并。")
    body = store.read("proj")
    assert "2026-09-15" in body
    assert "name: proj" in body
    with pytest.raises(FileToolError):
        store.read("nope")


def test_overwrite_updates_file_not_duplicates_index(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    assert store.save("m", "旧", "user", "旧内容").created is True
    result = store.save("m", "新 hook", "user", "新内容")
    assert result.created is False  # 覆盖旧条目

    assert "新内容" in store.read("m")
    assert "旧内容" not in store.read("m")
    index = (tmp_path / "mem" / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("m.md") == 1  # 不产生重复索引行
    assert "新 hook" in index


# ---------------------------------------------------------------------------
# list / 目录懒建
# ---------------------------------------------------------------------------


def test_list_newest_first_and_skips_index(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    store.save("first", "", "reference", "第一条")
    store.save("second", "较新", "user", "第二条")
    metas = store.list()
    names = [m.name for m in metas]
    assert names[0] == "second"  # 最新在前
    assert set(names) == {"first", "second"}
    assert all(meta.path.name != "MEMORY.md" for meta in metas)
    assert metas[0].description == "较新"
    assert metas[0].type_ == "user"


def test_list_on_missing_dir_returns_empty_and_does_not_mkdir(tmp_path: Path) -> None:
    target = tmp_path / "never"
    store = _store(target)
    assert store.list() == []
    assert store.index_text() == ""
    assert not target.exists()  # 读操作绝不建目录


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a b", "带中文", "", ".hidden", "MEMORY", "a/b"])
def test_save_rejects_bad_names(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path / "mem")
    with pytest.raises(FileToolError):
        store.save(bad, "", "user", "x")


def test_save_rejects_bad_type(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    with pytest.raises(FileToolError):
        store.save("ok_name", "", "banana", "x")


# ---------------------------------------------------------------------------
# index 截断
# ---------------------------------------------------------------------------


def test_index_text_truncates_oversized_index(tmp_path: Path) -> None:
    # list() 上限 200 条，故索引行数到不了 200+；能触达的截断是字节超限：
    # 200 行长 hook 累计 >25KB -> index_text 截断并附 WARNING。
    store = _store(tmp_path / "mem")
    for i in range(200):
        store.save(f"m{i:03d}", f"hook {i} " + "x" * 160, "user", f"内容 {i}")
    index_file = tmp_path / "mem" / "MEMORY.md"
    assert index_file.stat().st_size > 25_000  # 前提：索引确实超字节上限

    text = store.index_text()
    assert "WARNING" in text
    assert len(text.splitlines()) <= 200 + 3  # 截断后 ≤ 200 行 + 警告行数余量


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def test_resolve_memory_dir_override_and_home(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    monkeypatch.setenv("EMBERPY_MEMORY_DIR", str(tmp_path / "override"))
    assert resolve_memory_dir(workspace) == (tmp_path / "override").resolve()

    monkeypatch.delenv("EMBERPY_MEMORY_DIR")
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    got = resolve_memory_dir(workspace)
    assert got.parent.parent == (tmp_path / "home" / "projects").resolve()
    assert got.name == "memory"
    assert str(got).endswith(f"projects{workspace.name}-memory") or "projects" in str(got)


def test_memory_prompt_empty_then_with_index(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    empty = build_memory_prompt(store)
    assert "还没有任何记忆" in empty
    assert "MEMORY.md" in empty

    store.save("u", "喜欢中文", "user", "沟通用中文")
    full = build_memory_prompt(store)
    assert "喜欢中文" in full
    assert "memory_save" in full  # 行为引导在
