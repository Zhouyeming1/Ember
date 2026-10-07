"""字段级血缘：目标列 <- 源列（坑 1 的多语句切分、坑 2 的表名归一、坑 6 的一次拿全列）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import (
    DEFAULT_DIALECT,
    MAX_COLUMN_LINEAGE_STATEMENTS,
    SQLGLOT_AVAILABLE,
    _sqlglot_lineage,
    exp,
    table_name,
    StatementInfo,
)
from .parse import _QUERY_KINDS, parse_statements

# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnEdge:
    """目标列 <- 源列。"""

    target_table: str
    target_column: str
    source_table: str
    source_column: str
    via: str = ""     # 中间表达式（聚合/函数），便于人读


def _leaf_sources(node: Any, sink: list[tuple[str, str]], depth: int = 0) -> None:
    """递归收集 lineage 节点树的叶子 (源表, 源列)。

    叶子节点的 name 可能带限定前缀（实测是 `oi.amount` 这种），要去掉限定再取列名；
    源表走 table_name()（结构化字段），不能用 source.sql()——那会带上别名（坑 2）。
    """
    if depth > 40:
        return
    source = getattr(node, "source", None)
    if source is not None and not getattr(node, "downstream", None):
        table = table_name(source)
        if table:
            raw = str(getattr(node, "name", "") or "")
            column = raw.rsplit(".", 1)[-1].strip('"`[]')
            if column:
                sink.append((table, column))
    for child in getattr(node, "downstream", None) or []:
        _leaf_sources(child, sink, depth + 1)


def column_edges(text: str, dialect: str = DEFAULT_DIALECT) -> list[ColumnEdge]:
    """抽出一个文件/一段 SQL 里的字段级血缘（目标列 <- 源列）。

    先按语句切分（坑 1：lineage 对多语句直接抛错），只对查询语句跑，且每条语句只
    调一次 lineage(column=None) 拿全部输出列（坑 6）。
    """
    if not SQLGLOT_AVAILABLE:
        return []
    statements, _ = parse_statements(text, dialect)
    edges: list[ColumnEdge] = []
    seen: set[tuple[str, str, str, str]] = set()
    budget = MAX_COLUMN_LINEAGE_STATEMENTS

    for stmt in statements:
        if stmt.kind not in _QUERY_KINDS or not stmt.target or budget <= 0:
            continue
        if stmt.kind == "query" and not stmt.target:
            continue
        budget -= 1
        # 只取"喂给 lineage 的查询体"：INSERT 的外层是 Insert 节点，lineage 要求
        # 输入是 SELECT，所以抠出它的 query 部分
        query_sql = _query_body_sql(stmt, dialect)
        if not query_sql:
            continue
        try:
            result = _sqlglot_lineage(None, query_sql, dialect=dialect)
        except Exception:
            # 单条语句的血缘失败不该让整个文件的字段级血缘归零，也不该冒成工具错误
            continue
        if not isinstance(result, dict):
            continue
        for out_column, node in result.items():
            leaves: list[tuple[str, str]] = []
            _leaf_sources(node, leaves)
            for table, column in leaves:
                key = (stmt.target, str(out_column).lower(), table, column.lower())
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    ColumnEdge(
                        target_table=stmt.target,
                        target_column=str(out_column).lower(),
                        source_table=table,
                        source_column=column.lower(),
                    )
                )
    return edges


def _query_body_sql(stmt: StatementInfo, dialect: str) -> str:
    """把一条语句规约成可喂给 lineage 的 SELECT 文本。"""
    tree = stmt.tree
    insert = tree.find(exp.Insert)
    if insert is not None:
        body = insert.expression
        if body is None:
            return ""
        return body.sql(dialect=dialect)
    create = tree.find(exp.Create)
    if create is not None and stmt.target:
        body = create.expression
        if body is None:
            return ""
        return body.sql(dialect=dialect)
    if tree.find(exp.Select) is not None:
        return tree.sql(dialect=dialect)
    return ""
