"""SQL 分析的共享底座：可选依赖判定、常量、表名归一、共享数据类。

同包内唯一不依赖其它模块的一层，parse / lineage / index / rules 都从这里取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:  # 可选依赖：装了就启用，没装也不影响引擎启动
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError
    from sqlglot.lineage import lineage as _sqlglot_lineage

    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于环境是否装了可选依赖
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]
    SqlglotError = Exception  # type: ignore[misc,assignment]
    _sqlglot_lineage = None  # type: ignore[assignment]
    SQLGLOT_AVAILABLE = False

MISSING_SQLGLOT_HINT = (
    "错误：未安装 sqlglot，SQL 分析工具不可用。请运行 pip install sqlglot"
    "（或 pip install emberpy[sql]）后重试。"
)

DEFAULT_DIALECT = "hive"
SQL_SUFFIXES = (".sql", ".hql")

# 索引硬上限（对齐 tools/fs.py 的量级）。超限时返回部分索引并显式标注"索引不完整"——
# 否则模型会把"没查到下游"当成"没有下游"，那是本功能最危险的静默错误。
MAX_INDEX_FILES = 2000
MAX_SQL_FILE_BYTES = 512 * 1024
PARSE_BUDGET_SECONDS = 5.0
# 字段级血缘每个文件最多跑多少条语句（lineage 要 qualify，比纯解析贵得多）。
MAX_COLUMN_LINEAGE_STATEMENTS = 20
MAX_IMPACT_DEPTH = 12

# ---------------------------------------------------------------------------
# 调度模板变量（坑 4）
# ---------------------------------------------------------------------------

_VAR_BODY = r"\$\{[^}]*\}|\{\{[^}]*\}\}|\$\[[^\]]*\]|\$[A-Za-z_][A-Za-z0-9_]*"
# 已被引号包住的变量（`'${bizdate}'`）整体换成合法字符串字面量，避免补引号补重
_QUOTED_VAR_RE = re.compile(rf"""(['"])\s*(?:{_VAR_BODY})\s*\1""")
_BARE_VAR_RE = re.compile(_VAR_BODY)
SCHED_VAR_PLACEHOLDER = "__EMBER_SCHED_VAR__"


def preprocess_templates(text: str) -> str:
    """把调度模板变量换成占位字符串字面量，让 sqlglot 能解析。

    替换不改行数（不带换行），所以 sqlglot 记的行号对原文件仍然有效。
    顺带一个好处：替换之后 `'__EMBER_SCHED_VAR__'` 不是日期形状，于是"硬编码日期"
    规则能天然区分"用了调度变量"和"写死了日期"。
    """
    text = _QUOTED_VAR_RE.sub(f"'{SCHED_VAR_PLACEHOLDER}'", text)
    return _BARE_VAR_RE.sub(f"'{SCHED_VAR_PLACEHOLDER}'", text)


# ---------------------------------------------------------------------------
# 表名归一（坑 2）
# ---------------------------------------------------------------------------


def table_name(table: Any) -> str:
    """exp.Table -> 归一化的 `库.表`（小写，不带别名/引号/分区子句）。

    这是整个血缘图能不能连通的关键：`FROM dws.dws_user_gmv g` 用 sql() 取会带上
    ` AS g`，于是 `dws.dws_user_gmv AS g` 和别处写的 `dws.dws_user_gmv` 变成两个
    不同的键，图上的边就断了——反向影响面会安静地少查一层。
    """
    if exp is None or not isinstance(table, exp.Table):
        return ""
    parts: list[str] = []
    for key in ("catalog", "db"):
        node = table.args.get(key)
        name = getattr(node, "name", None)
        if name:
            parts.append(str(name))
    base = getattr(table, "name", "") or ""
    if not base:
        return ""
    parts.append(str(base))
    return ".".join(parts).lower()


def split_table_name(name: str) -> tuple[Optional[str], str]:
    """`库.表` -> (库, 表)；无库名时库为 None。"""
    if "." in name:
        db, _, table = name.rpartition(".")
        return (db, table)
    return (None, name)


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementInfo:
    """一条语句的解析结果。"""

    kind: str                      # insert / ctas / query / ddl / other
    target: Optional[str]          # 写入的表（insert / ctas）
    sources: tuple[str, ...]       # 读的表（已剔除目标表自身）
    sql: str                       # 重序列化后的 SQL（喂给 lineage 用）
    tree: Any                      # sqlglot 表达式（lint 用，带 meta 行号）
    overwrite: bool = False        # INSERT OVERWRITE
    has_partition_clause: bool = False  # INSERT 写了 PARTITION (...)

    @property
    def line(self) -> int:
        meta = getattr(self.tree, "meta", None) or {}
        return int(meta.get("line") or 1)


@dataclass
class FileInfo:
    """一个 SQL 文件的解析结果。"""

    path: str                      # 相对工作区的 posix 路径
    abs_path: Path
    statements: tuple[StatementInfo, ...] = ()
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    error: Optional[str] = None    # 解析失败原因（该文件的血缘不纳入图）
    fingerprint: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class TableDef:
    """DDL 里读到的表定义（分区列是"缺分区过滤"规则的前提）。"""

    name: str
    columns: tuple[str, ...] = ()
    partition_columns: tuple[str, ...] = ()
    storage: Optional[str] = None
    file: str = ""
