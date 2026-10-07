"""规范与性能规则。每条规则带 rule_id + severity，可用 ignore 关闭。

规则取舍见 pyproject / 开发计划：静态判不出的一律不做（隐式类型转换、同表重复扫描、
大小表 join 顺序），宁可少几条也不制造满屏误报。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .base import (
    SQLGLOT_AVAILABLE,
    exp,
    table_name,
    FileInfo,
    StatementInfo,
    TableDef,
)

# ---------------------------------------------------------------------------

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# rule_id -> 一行说明。lint 输出末尾会列出来，让模型知道 ignore 能关掉什么。
RULE_SUMMARY: dict[str, str] = {
    "cartesian-join": "笛卡尔积（无 ON 的 JOIN / 逗号连接 / CROSS JOIN）",
    "partition-column-wrapped": "分区列被函数包裹，打掉分区裁剪",
    "like-leading-wildcard": "LIKE 以前置通配符开头，全表扫描",
    "hardcoded-date": "分区过滤里写死了日期，调度重跑不换日期",
    "overwrite-without-partition": "INSERT OVERWRITE 分区表但没写 PARTITION，覆盖全部分区",
    "self-read-write": "INSERT 的目标表同时是源表（自读自写）",
    "missing-partition-filter": "读了分区表但整条语句没有分区过滤，扫全部分区",
    "select-star": "多表 join 最外层用 SELECT *",
}

# 比较类节点：命中这些才算"过滤"，光出现列名不算
_COMPARISONS: tuple[Any, ...] = tuple(
    getattr(exp, name)
    for name in ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Between", "In", "Like", "Is")
    if exp is not None and hasattr(exp, name)
)
_BOOLEAN_CLASS = getattr(exp, "Boolean", None) if exp is not None else None

_DATE_LITERAL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T].*)?$"      # 2024-01-01
    r"|^\d{8}$"                          # 20240101
    r"|^\d{6}$"                          # 202401
    r"|^\d{4}-\d{2}$"                    # 2024-01
)
# 无 DDL 可依时的兜底：列名长得像分区列
_PARTITIONISH = {
    "dt", "ds", "pt", "date", "day", "hour", "month", "year",
    "bizdate", "stat_date", "log_date", "part_dt", "partition_date",
}
# 会打掉分区裁剪的日期函数
_PARTITION_WRAPPERS = {
    "substr", "substring", "to_date", "date_format", "date_sub", "date_add",
    "year", "month", "day", "hour", "trunc", "cast", "from_unixtime",
}


@dataclass(frozen=True)
class LintIssue:
    rule_id: str
    severity: str
    message: str
    file: str = ""
    line: int = 0
    detail: str = ""


def _line_of(node: Any, depth: int = 24) -> int:
    """取节点所在行号。

    坑：sqlglot 的行号 meta 只挂在 **token 级**节点上（Identifier / Substring /
    Star / Literal），结构性节点（Join / Insert / Select / EQ）的 meta 是空的。
    直接读 node.meta['line'] 会全部拿到 0，lint 输出变成「:0」——看着有定位、
    其实是废的。所以往子树里找第一个带行号的 token。
    """
    if node is None:
        return 0
    meta = getattr(node, "meta", None) or {}
    try:
        line = int(meta.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    if line:
        return line
    if depth <= 0:
        return 0
    for child in node.iter_expressions():
        found = _line_of(child, depth - 1)
        if found:
            return found
    return 0


def _is_true_literal(node: Any) -> bool:
    """`JOIN b`（无 ON）被 sqlglot 规范化成 `ON TRUE`——坑 3 的另一半。"""
    return _BOOLEAN_CLASS is not None and isinstance(node, _BOOLEAN_CLASS) and node.this is True


def _literal_text(node: Any) -> str:
    value = getattr(node, "this", None)
    return str(value) if isinstance(value, str) else ""


def _wrapped_partition_column(node: Any, known_partitions: set[str]) -> Optional[str]:
    """`substr(dt,1,6)` / `to_date(dt)` 这类：返回被包裹的列名。

    只在两种情况下认：该列在 DDL 里确实是分区列；或没有 DDL 时列名长得像分区列
    （兜底，会有少量误报，所以在输出里注明依据）。
    """
    func = None
    for candidate in ("Substring", "Anonymous", "ToDate", "Cast", "Year", "Month", "Day", "Hour"):
        cls = getattr(exp, candidate, None)
        if cls is not None and isinstance(node, cls):
            func = node
            break
    if func is None:
        return None
    func_name = ""
    if isinstance(func, exp.Anonymous):
        func_name = str(func.this or "").lower()
    elif isinstance(func, exp.Cast):
        return None  # cast 单独判，不在此列
    else:
        func_name = type(func).__name__.lower()
    if func_name and func_name not in _PARTITION_WRAPPERS:
        return None
    for col in func.find_all(exp.Column):
        name = (col.name or "").lower()
        if not name:
            continue
        table = table_name(col.args.get("table")) if isinstance(col.args.get("table"), exp.Table) else ""
        if name in known_partitions or (not known_partitions and name in _PARTITIONISH):
            return f"{table + '.' if table else ''}{name}"
    return None


def lint_statements(
    info: FileInfo,
    catalog: Optional[dict[str, TableDef]] = None,
    ignore: Optional[Iterable[str]] = None,
) -> list[LintIssue]:
    """对已解析的文件跑全部规则。catalog 缺省时依赖 DDL 的规则自动静默。"""
    if not SQLGLOT_AVAILABLE:
        return []
    skip = {str(rule).strip() for rule in (ignore or ()) if str(rule).strip()}
    catalog = catalog or {}
    issues: list[LintIssue] = []

    def add(rule_id: str, severity: str, message: str, node: Any = None, detail: str = "") -> None:
        if rule_id in skip:
            return
        issues.append(
            LintIssue(
                rule_id=rule_id,
                severity=severity,
                message=message,
                file=info.path,
                line=_line_of(node) if node is not None else 0,
                detail=detail,
            )
        )

    for stmt in info.statements:
        tree = stmt.tree
        # ---- 笛卡尔积（坑 3：两种形态都要认）----
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            kind = str(join.args.get("kind") or "").upper()
            if kind == "CROSS":
                add(
                    "cartesian-join",
                    SEVERITY_WARNING,
                    "CROSS JOIN（或逗号连接）会让两侧行数相乘，通常不是本意",
                    join,
                    detail="两侧有过滤条件时应改为显式 JOIN ... ON",
                )
            elif on is not None and _is_true_literal(on):
                add(
                    "cartesian-join",
                    SEVERITY_WARNING,
                    "JOIN 没有 ON 条件（等价于 ON TRUE，笛卡尔积）",
                    join,
                    detail="补上关联键，或确属有意为之就写成 CROSS JOIN",
                )

        # ---- 自读自写 ----
        if stmt.target and stmt.target in stmt.sources:
            add(
                "self-read-write",
                SEVERITY_WARNING,
                f"INSERT 的目标表 {stmt.target} 同时是源表（自读自写）",
                tree,
                detail="Hive 下行为依赖执行顺序，容易读到写了一半的数据；建议落中间表再换入",
            )

        # ---- INSERT OVERWRITE 未指定分区 ----
        if stmt.kind == "insert" and stmt.overwrite and stmt.target and not stmt.has_partition_clause:
            table_def = catalog.get(stmt.target)
            if table_def is not None and table_def.partition_columns:
                add(
                    "overwrite-without-partition",
                    SEVERITY_ERROR,
                    f"INSERT OVERWRITE {stmt.target} 未指定 PARTITION，而该表有分区列 "
                    f"({'/'.join(table_def.partition_columns)})",
                    tree,
                    detail="这会覆盖全部分区（破坏性），应写成 PARTITION(...)",
                )

        # ---- 分区列被函数包裹 ----
        partition_cols_all: set[str] = set()
        for table in stmt.sources:
            table_def = catalog.get(table)
            if table_def is not None:
                partition_cols_all.update(table_def.partition_columns)
        for comparison in tree.find_all(*_COMPARISONS) if _COMPARISONS else []:
            for side in (comparison.this, comparison.expression):
                if side is None:
                    continue
                wrapped = _wrapped_partition_column(side, partition_cols_all)
                if wrapped:
                    add(
                        "partition-column-wrapped",
                        SEVERITY_WARNING,
                        f"分区列被函数包裹（{wrapped}），会导致分区裁剪失效、扫描全表",
                        comparison,
                        detail="把分区列单独放在一侧，例如 dt = '20240101' 而不是 substr(dt,1,6)='202401'",
                    )
                    break

        # ---- 硬编码日期 ----
        # 日期形状的字面量比在"分区列"上才算：写死的日期本身不是错（一次性回刷就很
        # 正常），写死在调度脚本的分区过滤里才是事故。分区列来源两处：catalog 里
        # 确实声明的分区列，以及没有 DDL 时的列名兜底。
        date_columns = partition_cols_all | _PARTITIONISH
        for literal in tree.find_all(exp.Literal):
            text = _literal_text(literal)
            if not text or not _DATE_LITERAL_RE.match(text):
                continue
            parent = literal.parent
            if not _COMPARISONS or not isinstance(parent, _COMPARISONS):
                continue
            column = None
            for candidate in (parent.this, parent.expression):
                if isinstance(candidate, exp.Column):
                    column = (candidate.name or "").lower()
                    break
            if column and column in date_columns:
                add(
                    "hardcoded-date",
                    SEVERITY_WARNING,
                    f"分区过滤写死了日期（{column} = '{text}'），调度重跑不会换日期",
                    literal,
                    detail="应使用调度变量（如 ${bizdate} / $[yyyyMMdd]）",
                )

        # ---- like 前置通配符 ----
        for like in tree.find_all(exp.Like):
            pattern = _literal_text(like.expression)
            if pattern.startswith("%"):
                add(
                    "like-leading-wildcard",
                    SEVERITY_INFO,
                    f"LIKE '{pattern}' 以前置通配符开头，无法走索引、会全表扫描",
                    like,
                )

        # ---- select *（只在 join 的最外层投影）----
        outermost = _outermost_select(stmt)
        if outermost is not None and tree.find(exp.Join) is not None:
            if any(isinstance(item, exp.Star) for item in outermost.expressions):
                add(
                    "select-star",
                    SEVERITY_INFO,
                    "多表 join 的最外层用 SELECT *，列名歧义/列爆炸风险",
                    outermost,
                    detail="建议显式列出需要的列",
                )

        # ---- 缺分区过滤 ----
        for table in stmt.sources:
            table_def = catalog.get(table)
            if table_def is None or not table_def.partition_columns:
                continue
            if _has_partition_filter(tree, set(table_def.partition_columns)):
                continue
            add(
                "missing-partition-filter",
                SEVERITY_WARNING,
                f"读取分区表 {table}（分区列 {'/'.join(table_def.partition_columns)}）"
                "但整条语句里没有分区过滤条件",
                tree,
                detail="会扫描该表全部分区",
            )

    return issues


def _outermost_select(stmt: StatementInfo) -> Any:
    tree = stmt.tree
    insert = tree.find(exp.Insert)
    if insert is not None and insert.expression is not None:
        return insert.expression.find(exp.Select) or (
            insert.expression if isinstance(insert.expression, exp.Select) else None
        )
    create = tree.find(exp.Create)
    if create is not None and create.expression is not None:
        return create.expression.find(exp.Select)
    if isinstance(tree, exp.Select):
        return tree
    return tree.find(exp.Select)


def _has_partition_filter(tree: Any, partition_columns: set[str]) -> bool:
    """整条语句里有没有对这些分区列的过滤条件。

    刻意做整语句扫描（不做 scope 精确分析）：分区过滤常常写在包住它的 CTE / 子查询
    里，整语句扫recall 更高、误报更少。代价是可能有少量漏报（子查询里的过滤其实
    不作用于这张表），对一条 warning 级规则是可接受的取舍。
    """
    if not _COMPARISONS:
        return False
    for comparison in tree.find_all(*_COMPARISONS):
        for side in (comparison.this, comparison.expression):
            if isinstance(side, exp.Column) and (side.name or "").lower() in partition_columns:
                return True
            # substr(dt,..) 这类虽然打掉裁剪，但"确实有过滤"，不算缺过滤
            if _wrapped_partition_column(side, partition_columns):
                return True
    return False
