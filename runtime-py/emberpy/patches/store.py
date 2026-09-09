"""文件整写补丁与 checkpoint（PatchStore 的实现）。

思路对应 Ember 前端的 ember-checkpoint：每个文件写操作都先记录"改前内容"，
因此可以在不依赖 git 的情况下按轮 / 按路径回滚（/undo）。

v1 采用"整文件内容"补丁而不是行级 diff——实现简单、语义清晰、便于测试；
后续可平滑升级为行级 edit，checkpoint 的 before/after 语义不变。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..errors import FileToolError, NotTextFileError


@dataclass(frozen=True)
class Patch:
    """一次文件写操作的快照。

    - before: 原内容；文件原本不存在则为 None
    - after:  新内容；None 表示删除文件
    - mode:   文件权限位（可选）
    """

    seq: int
    path: Path
    before: Optional[str]
    after: Optional[str]
    mode: Optional[int] = None

    @property
    def summary(self) -> str:
        action = "新建" if self.before is None else ("删除" if self.after is None else "修改")
        return f"{action} {self.path.name}"

    def changed_lines(self) -> int:
        """粗略行数变化（仅用于展示）。"""
        b = 0 if self.before is None else self.before.count("\n") + 1
        a = 0 if self.after is None else self.after.count("\n") + 1
        return abs(a - b)


def _read_text(path: Path) -> Optional[str]:
    """读文件为 UTF-8 文本；不存在返回 None；非 UTF-8 抛 NotTextFileError。"""
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - 平台相关
        raise FileToolError(f"读取失败：{exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotTextFileError(f"文件不是 UTF-8 文本，拒绝整写：{path}") from exc


class PatchStore:
    """工作区内全部文件写操作的 checkpoint 栈。

    注意：这里只保证"本进程运行期间"可回滚；跨崩溃恢复的 undo 属于 v2，
    到时把 Patch 序列化进会话日志即可重建（before/after 语义已经准备好）。
    """

    def __init__(self) -> None:
        self._history: list[Patch] = []
        self._seq = 0

    # -- 记录与执行 ------------------------------------------------------
    def apply_write(self, path: Path, content: str) -> Patch:
        """把 content 整写进 path，并记录一条 checkpoint。返回 Patch。"""
        if "\x00" in content:
            raise NotTextFileError(f"内容包含 NUL 字节，拒绝写入：{path}")
        before = _read_text(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        patch = Patch(self._seq, path, before, content)
        self._history.append(patch)
        try:
            path.write_text(content, encoding="utf-8", newline="\n")
            if patch.mode is not None:
                os.chmod(path, patch.mode)
        except OSError as exc:
            # 写入失败：从历史里回滚该条，避免留下"已记录但没生效"的假象
            if self._history and self._history[-1] is patch:
                self._history.pop()
            raise FileToolError(f"写入失败：{exc}") from exc
        return patch

    def apply_edit(
        self,
        path: Path,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Patch:
        """在已有文件上做一处（或 replace_all 全部）精确文本替换，并记录 checkpoint。

        与 apply_write 同语义：before = 改前全文、after = 改后全文，/undo 整文件还原。
        原子性：先校验全部条件（文件存在、old 非空、old!=new、能唯一匹配）再写盘，
        任何失败都不进历史栈、文件原样。

        校验失败的报错都面向"让模型自己修正"：提示先 read_file 确认、扩大上下文，
        或设 replace_all=true。
        """
        if old_string == "":
            raise FileToolError("old_string 不能为空，请提供要替换的原文片段")
        if "\x00" in new_string:
            raise NotTextFileError("new_string 包含 NUL 字节，拒绝写入")
        if new_string == old_string:
            raise FileToolError("old_string 与 new_string 相同，无需修改")
        text = _read_text(path)
        if text is None:
            raise FileToolError(f"文件不存在，无法编辑：{path}；新建文件请用 write_file 整写")
        count = text.count(old_string)
        if count == 0:
            raise FileToolError(
                f"在 {path} 里找不到要替换的 old_string，可能文件内容与你预期不同；"
                "请先 read_file 确认当前内容再编辑"
            )
        if count > 1 and not replace_all:
            raise FileToolError(
                f"old_string 在 {path} 里出现 {count} 次，无法唯一确定替换位置："
                "请把 old_string 写长到唯一匹配，或设 replace_all=true"
            )
        after = new_string.join(text.split(old_string)) if replace_all else text.replace(old_string, new_string, 1)
        self._seq += 1
        patch = Patch(self._seq, path, text, after)
        self._history.append(patch)
        try:
            path.write_text(after, encoding="utf-8", newline="\n")
        except OSError as exc:
            if self._history and self._history[-1] is patch:
                self._history.pop()
            raise FileToolError(f"写入失败：{exc}") from exc
        return patch

    def apply_delete(self, path: Path) -> Patch:
        """删除文件（也会记录 checkpoint，可回滚恢复内容）。"""
        before = _read_text(path)
        if before is None:
            raise FileToolError(f"文件不存在：{path}")
        self._seq += 1
        patch = Patch(self._seq, path, before, None)
        self._history.append(patch)
        try:
            path.unlink()
        except OSError as exc:
            if self._history and self._history[-1] is patch:
                self._history.pop()
            raise FileToolError(f"删除失败：{exc}") from exc
        return patch

    # -- 回滚 ------------------------------------------------------------
    def undo_last(self) -> Optional[Patch]:
        """回滚最近一次写操作，返回被回滚的 Patch；无操作返回 None。"""
        if not self._history:
            return None
        patch = self._history.pop()
        self._restore(patch)
        return patch

    def undo_path(self, path: Path) -> Optional[Patch]:
        """回滚指定路径最近一次写操作。"""
        for index in range(len(self._history) - 1, -1, -1):
            patch = self._history[index]
            if patch.path == path:
                self._history.pop(index)
                self._restore(patch)
                return patch
        return None

    def _restore(self, patch: Patch) -> None:
        if patch.before is None:
            if patch.path.exists():
                patch.path.unlink()
        else:
            patch.path.parent.mkdir(parents=True, exist_ok=True)
            patch.path.write_text(patch.before, encoding="utf-8", newline="\n")

    # -- 查询 ------------------------------------------------------------
    @property
    def high_water(self) -> int:
        """已发出的最大补丁序号（供 compact 后把同步水位抬高，避免旧补丁重放）。"""
        return self._seq

    def recent(self, limit: int = 20) -> list[Patch]:
        return self._history[-limit:]

    def since_seq(self, min_seq: int) -> list[Patch]:
        """返回 seq > min_seq 的全部补丁（按时间序）。

        undo 会弹栈但不会复用 seq，因此以 seq 为准做增量同步是准确的——
        即使中途 undo 过，新写入的补丁 seq 仍单调递增。
        """
        return [patch for patch in self._history if patch.seq > min_seq]

    def __iter__(self) -> Iterator[Patch]:
        return iter(self._history)

    def __len__(self) -> int:
        return len(self._history)
