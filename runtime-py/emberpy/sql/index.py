"""工作区索引：文件 -> 读写的表 -> 正反向依赖图，含缓存与失效。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .base import (
    DEFAULT_DIALECT,
    MAX_IMPACT_DEPTH,
    MAX_INDEX_FILES,
    MAX_SQL_FILE_BYTES,
    PARSE_BUDGET_SECONDS,
    SQL_SUFFIXES,
    split_table_name,
    FileInfo,
    TableDef,
)
from .lineage import ColumnEdge, column_edges
from .parse import analyze_text, parse_table_defs

# ---------------------------------------------------------------------------


@dataclass
class SqlIndex:
    """工作区级 SQL 血缘索引：文件 -> 读写的表 -> 正/反向依赖图。"""

    workspace: Path
    dialect: str = DEFAULT_DIALECT
    files: dict[str, FileInfo] = field(default_factory=dict)
    catalog: dict[str, TableDef] = field(default_factory=dict)
    forward: dict[str, set[str]] = field(default_factory=dict)
    reverse: dict[str, set[str]] = field(default_factory=dict)
    column_edges: list[ColumnEdge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    built_at: float = 0.0

    # -- 构建 ---------------------------------------------------------------

    def refresh(self, gate: Any = None) -> None:
        """全量 walk + 按 (mtime, size) 增量解析。"""
        started = time.monotonic()
        self.notes = []
        found: list[tuple[str, Path, tuple[int, int]]] = []
        truncated = False
        for path in _walk_sql_files(self.workspace, gate):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size > MAX_SQL_FILE_BYTES:
                self.notes.append(
                    f"{_rel(self.workspace, path)} 超过 {MAX_SQL_FILE_BYTES // 1024}KB，已跳过"
                )
                continue
            if len(found) >= MAX_INDEX_FILES:
                truncated = True
                break
            found.append((_rel(self.workspace, path), path, (st.st_mtime_ns, st.st_size)))

        seen_paths = set()
        for rel, abs_path, fingerprint in found:
            seen_paths.add(rel)
            cached = self.files.get(rel)
            if cached is not None and cached.fingerprint == fingerprint:
                continue
            self.files[rel] = self._parse_one(rel, abs_path, fingerprint)
            if time.monotonic() - started > PARSE_BUDGET_SECONDS:
                truncated = True
                break
        # 删掉已消失的文件（否则改名/删除后旧边永远留在图上）
        for rel in [r for r in self.files if r not in seen_paths]:
            self.files.pop(rel, None)

        self._rebuild()
        if truncated:
            self.notes.append(
                f"索引不完整：已扫描 {len(self.files)} 个文件后达到上限/超时，"
                "结果可能漏掉下游。可用 path 参数只分析单个文件。"
            )
        failed = [rel for rel, info in self.files.items() if info.error]
        if failed:
            shown = "、".join(sorted(failed)[:5])
            more = f" 等 {len(failed)} 个" if len(failed) > 5 else ""
            self.notes.append(f"以下文件解析失败，其血缘未纳入：{shown}{more}")
        self.built_at = time.monotonic()

    def _parse_one(self, rel: str, abs_path: Path, fingerprint: tuple[int, int]) -> FileInfo:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return FileInfo(path=rel, abs_path=abs_path, error=str(exc), fingerprint=fingerprint)
        info = analyze_text(text, self.dialect, rel)
        reads = list(info.reads)
        writes = list(info.writes)
        for stmt in info.statements:
            for src in stmt.sources:
                if src not in reads:
                    reads.append(src)
        return FileInfo(
            path=rel,
            abs_path=abs_path,
            statements=info.statements,
            reads=frozenset(reads),
            writes=frozenset(writes),
            error=info.error,
            fingerprint=fingerprint,
        )

    def _rebuild(self) -> None:
        self.forward = {}
        self.reverse = {}
        self.catalog = {}
        edges: list[ColumnEdge] = []
        for rel, info in sorted(self.files.items()):
            if info.error:
                continue
            try:
                text = info.abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for table_def in parse_table_defs(text, self.dialect):
                table_def = TableDef(
                    name=table_def.name,
                    columns=table_def.columns,
                    partition_columns=table_def.partition_columns,
                    storage=table_def.storage,
                    file=rel,
                )
                self.catalog.setdefault(table_def.name, table_def)
            for stmt in info.statements:
                if not stmt.target:
                    continue
                for src in stmt.sources:
                    if src == stmt.target:
                        continue
                    self.forward.setdefault(src, set()).add(stmt.target)
                    self.reverse.setdefault(stmt.target, set()).add(src)
            edges.extend(column_edges(text, self.dialect))
        # 字段级边去重（跨文件重复的很常见）
        uniq: dict[tuple[str, str, str, str], ColumnEdge] = {}
        for edge in edges:
            uniq.setdefault(
                (edge.target_table, edge.target_column, edge.source_table, edge.source_column), edge
            )
        self.column_edges = list(uniq.values())

    # -- 查询 ---------------------------------------------------------------

    def match_tables(self, name: str) -> tuple[list[str], Optional[str]]:
        """把用户给的表名解析成索引里的全名。

        未限定库名的 `orders` 在 Hive 里指当前库，但索引键是 `dws.orders`，对不上。
        这里按后缀匹配、recall 优先，并把匹配方式回给调用方（结果里要注明）。
        """
        wanted = name.strip().strip('"`[]').lower()
        if not wanted:
            return ([], None)
        known = set(self.forward) | set(self.reverse) | set(self.catalog)
        if wanted in known:
            return ([wanted], None)
        _, tail = split_table_name(wanted)
        hits = sorted(t for t in known if split_table_name(t)[1] == tail)
        if hits:
            return (hits, f"“{name}”未带库名，按表名后缀匹配到 {len(hits)} 张表")
        return ([], None)

    def downstream(self, table: str, max_depth: int = MAX_IMPACT_DEPTH) -> list[list[str]]:
        """按层返回下游表（BFS），含环保护。"""
        layers: list[list[str]] = []
        seen = {table}
        frontier = [table]
        for _ in range(max_depth):
            nxt: list[str] = []
            for node in frontier:
                for child in sorted(self.forward.get(node, ())):
                    if child in seen:
                        continue
                    seen.add(child)
                    nxt.append(child)
            if not nxt:
                break
            layers.append(nxt)
            frontier = nxt
        return layers

    def upstream(self, table: str, max_depth: int = MAX_IMPACT_DEPTH) -> list[list[str]]:
        """按层返回上游表（BFS）。"""
        layers: list[list[str]] = []
        seen = {table}
        frontier = [table]
        for _ in range(max_depth):
            nxt: list[str] = []
            for node in frontier:
                for parent in sorted(self.reverse.get(node, ())):
                    if parent in seen:
                        continue
                    seen.add(parent)
                    nxt.append(parent)
            if not nxt:
                break
            layers.append(nxt)
            frontier = nxt
        return layers

    def files_reading(self, table: str) -> list[str]:
        return sorted(rel for rel, info in self.files.items() if table in info.reads)

    def files_writing(self, table: str) -> list[str]:
        return sorted(rel for rel, info in self.files.items() if table in info.writes)

    def downstream_columns(self, table: str, column: Optional[str] = None) -> list[ColumnEdge]:
        """字段级影响面：谁直接读了这个表的这个字段。"""
        out = []
        for edge in self.column_edges:
            if edge.source_table != table:
                continue
            if column and edge.source_column != column.lower():
                continue
            out.append(edge)
        return out

    def downstream_column_chain(
        self, table: str, column: str, max_depth: int = MAX_IMPACT_DEPTH
    ) -> list[list[tuple[str, str]]]:
        """字段级影响面按层接力：amount -> dws.gmv -> ads.total_gmv。

        只查一层会漏掉间接影响面，而"下游字段改了口径"往往正是隔了一两层才炸——
        改 ODS 的 amount，真正被业务看见的是 ADS 那个叫 total_gmv 的字段。
        """
        start = (table, column.lower())
        seen = {start}
        frontier = [start]
        layers: list[list[tuple[str, str]]] = []
        for _ in range(max_depth):
            nxt: list[tuple[str, str]] = []
            for tbl, col in frontier:
                for edge in self.column_edges:
                    if edge.source_table != tbl or edge.source_column != col:
                        continue
                    key = (edge.target_table, edge.target_column)
                    if key in seen:
                        continue
                    seen.add(key)
                    nxt.append(key)
            if not nxt:
                break
            layers.append(sorted(nxt))
            frontier = nxt
        return layers


def _rel(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _walk_sql_files(workspace: Path, gate: Any = None) -> Iterable[Path]:
    """列出工作区里的 SQL 文件。

    跳过目录复用 tools/fs.py 的 SCAN_SKIP_DIRS（.git/node_modules/.ember 等）；
    并按权限门的 path deny 规则过滤——工作区内 authorize_read 是无条件放行的，唯一
    能按文件拦读的是 settings.json 里的 Read(...) deny，直接 rglob 会绕过它。
    """
    from ..tools.fs import SCAN_SKIP_DIRS  # 局部导入：避免模块级循环依赖

    policy = getattr(getattr(gate, "policy", None), "denies", None)
    stack = [workspace]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name in SCAN_SKIP_DIRS:
                        continue
                    stack.append(entry)
                elif entry.suffix.lower() in SQL_SUFFIXES:
                    if callable(policy) and policy(entry):
                        continue
                    yield entry
            except OSError:
                continue


_INDEX_CACHE: dict[str, SqlIndex] = {}


def get_index(workspace: Path, gate: Any = None, dialect: str = DEFAULT_DIALECT) -> SqlIndex:
    """取工作区索引（模块级缓存，按 resolve 后的路径键控）。

    放模块级而不是挂 ToolEnv：子 agent 的 child_env 是全新 ToolEnv、看不到父 env 的
    任何属性，但按 workspace 键控的缓存会命中，父与子共享同一份；同时 tmp_path 测试
    天然按目录隔离，不需要 reset 钩子，也不用改 tests/ 里 20+ 处 ToolEnv 构造点。
    """
    key = str(Path(workspace).resolve())
    index = _INDEX_CACHE.get(key)
    if index is None or index.dialect != dialect:
        index = SqlIndex(workspace=Path(workspace).resolve(), dialect=dialect)
        _INDEX_CACHE[key] = index
    index.refresh(gate=gate)
    return index


def reset_index_cache() -> None:
    """清空索引缓存（测试用）。"""
    _INDEX_CACHE.clear()
