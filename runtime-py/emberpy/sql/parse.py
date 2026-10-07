"""SQL 解析：模板变量预替换、多语句切分、表/分区定义提取（坑 1、坑 4）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .base import (
    DEFAULT_DIALECT,
    SQLGLOT_AVAILABLE,
    SqlglotError,
    exp,
    preprocess_templates,
    sqlglot,
    table_name,
    FileInfo,
    StatementInfo,
    TableDef,
)

# ---------------------------------------------------------------------------

_QUERY_KINDS = ("insert", "ctas", "query")


def create_target_node(create: Any) -> Any:
    """CREATE 语句的目标 Table 节点。

    坑：`create.this` 在带列定义时是 `exp.Schema`（`this` 才是 `exp.Table`），
    只有 CTAS/无列定义时才是 `exp.Table` 本身。直接把 create.this 丢给 table_name()
    会拿到空串——表现是 DDL 目录整个是空的，依赖 DDL 的规则（缺分区过滤、
    INSERT OVERWRITE 未指定分区）静默失效，而且不报任何错。
    """
    node = create.this
    if isinstance(node, exp.Schema):
        node = node.this
    return node


def has_partition_clause(node: Any) -> bool:
    """INSERT 的目标 Table 上有没有真正的 PARTITION 子句。

    坑：`Insert.args['partition']` 这个键**总是存在**，缺省值是布尔 `False`（来自
    arg_types 的占位），真正写的子句挂在 `Insert.this.args['partition']` 上、类型是
    `exp.Partition`。所以判 `insert.args.get("partition") is not None` 恒为真，
    "INSERT OVERWRITE 未指定 PARTITION" 这条规则会永不触发，且不报任何错——
    一个破坏性操作检查就这样静默失效了。
    """
    if not isinstance(node, exp.Table):
        return False
    part = node.args.get("partition")
    return part is not None and not isinstance(part, bool)


def _classify(tree: Any) -> tuple[str, Optional[str], bool, bool, Any]:
    """判定语句类型 -> (kind, 目标表, overwrite, 有无 PARTITION 子句, 目标节点)。

    kind: insert / ctas / query / ddl / other
    """
    insert = tree.find(exp.Insert)
    if insert is not None:
        node = insert.this
        name = table_name(node) if isinstance(node, exp.Table) else ""
        return ("insert", name or None, bool(insert.args.get("overwrite")), has_partition_clause(node), node)

    create = tree.find(exp.Create)
    if create is not None:
        kind_arg = str(create.args.get("kind") or "").upper()
        node = create_target_node(create)
        name = table_name(node)
        body = create.expression
        has_query = body is not None and body.find(exp.Select) is not None
        # CTAS 与 CREATE VIEW 都是"由查询定义出的派生表"，算血缘边；别漏掉 VIEW，
        # 数仓里视图很常见
        if kind_arg in ("TABLE", "VIEW") and has_query:
            return ("ctas", name or None, False, False, node)
        # 纯 DDL：目标记在 writes 里（便于"谁定义/写了这张表"），但 sources 为空，
        # 于是 _rebuild 不会为它建边
        return ("ddl", (name or None) if kind_arg == "TABLE" else None, False, False, node)

    if tree.find(exp.Select) is not None:
        return ("query", None, False, False, None)
    return ("other", None, False, False, None)


def _cte_names(tree: Any) -> set[str]:
    """语句里定义的 CTE 名。

    CTE 的引用在 AST 里也是 exp.Table 节点，不排除就会被当成真实表混进血缘图——
    `WITH order_agg AS (...)` 之后 FROM order_agg 会凭空造出一张叫 order_agg 的表。
    假表本身危害有限，但它会：让"索引里没有这张表"的判断串味、在 CTE 名与别处真实
    表名撞车时连出一条错误的边，且影响面输出里混进一串不存在的东西。
    """
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(str(alias).lower())
    return names


def _sources_of(tree: Any, target_node: Any) -> tuple[str, ...]:
    """语句里读到的全部表（剔除目标表节点本身与语句内定义的 CTE）。"""
    ctes = _cte_names(tree)
    names: list[str] = []
    for node in tree.find_all(exp.Table):
        if target_node is not None and node is target_node:
            continue
        base = (node.name or "").lower()
        # 只有"无限定名 + 命中本语句 CTE 名"才排除：带库名的 dws.order_agg 是真表
        if base in ctes and node.args.get("db") is None and node.args.get("catalog") is None:
            continue
        name = table_name(node)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def parse_statements(text: str, dialect: str = DEFAULT_DIALECT) -> tuple[list[StatementInfo], Optional[str]]:
    """解析一段 SQL 文本 -> (语句列表, 错误信息)。

    先做模板变量预替换（坑 4），再整段 parse。**不用 error_level=IGNORE**：宁可把
    整个文件标成"解析失败"并显式告知，也不静默吞掉语句——静默漏语句会直接变成
    血缘图漏边，正是本功能最怕的那种错。
    """
    if not SQLGLOT_AVAILABLE:
        return ([], "未安装 sqlglot")
    prepared = preprocess_templates(text)
    try:
        trees = sqlglot.parse(prepared, read=dialect)
    except SqlglotError as exc:
        return ([], str(exc))
    except Exception as exc:  # sqlglot 偶尔抛非 SqlglotError（如 TokenError 子类）
        return ([], str(exc))

    out: list[StatementInfo] = []
    for tree in trees:
        if tree is None:
            continue
        kind, target, overwrite, has_partition, target_node = _classify(tree)
        sources = _sources_of(tree, target_node)
        # 目标表可能同时出现在 FROM 里（自读自写），从 sources 里保留它——这条正是
        # self-read-write 规则要看的
        try:
            sql_text = tree.sql(dialect=dialect)
        except Exception:
            sql_text = ""
        out.append(
            StatementInfo(
                kind=kind,
                target=target,
                sources=sources,
                sql=sql_text,
                tree=tree,
                overwrite=overwrite,
                has_partition_clause=has_partition,
            )
        )
    return (out, None)


def parse_table_defs(text: str, dialect: str = DEFAULT_DIALECT) -> list[TableDef]:
    """从 DDL 里提取表定义（列 / 分区列 / 存储格式）。

    分区列是"缺分区过滤"和"INSERT OVERWRITE 未指定分区"两条规则的前提。没有对应
    DDL 的表，这两条规则对它就静默禁用——不猜。
    """
    if not SQLGLOT_AVAILABLE:
        return []
    try:
        trees = sqlglot.parse(preprocess_templates(text), read=dialect)
    except Exception:
        return []

    defs: list[TableDef] = []
    for tree in trees:
        create = tree.find(exp.Create)
        if create is None or str(create.args.get("kind") or "").upper() != "TABLE":
            continue
        name = table_name(create_target_node(create))
        if not name:
            continue
        schema = create.this if isinstance(create.this, exp.Schema) else None
        columns: list[str] = []
        if schema is not None:
            for col in schema.expressions:
                if isinstance(col, exp.ColumnDef) and col.name:
                    columns.append(col.name.lower())
        partitions: list[str] = []
        storage: Optional[str] = None
        props = create.args.get("properties")
        if props is not None:
            for prop in props.expressions:
                if isinstance(prop, exp.PartitionedByProperty):
                    inner = prop.this
                    exprs = getattr(inner, "expressions", None) or []
                    for col in exprs:
                        col_name = getattr(col, "name", None) or getattr(
                            getattr(col, "this", None), "name", None
                        )
                        if col_name:
                            partitions.append(str(col_name).lower())
                elif type(prop).__name__ == "FileFormatProperty":
                    value = prop.args.get("this")
                    storage = str(getattr(value, "name", value) or "") or None
        defs.append(
            TableDef(
                name=name,
                columns=tuple(columns),
                partition_columns=tuple(partitions),
                storage=storage,
            )
        )
    return defs


def analyze_text(text: str, dialect: str = DEFAULT_DIALECT, path: str = "<inline>") -> FileInfo:
    """解析一段 SQL 文本成 FileInfo（不做工作区索引）。"""
    statements, error = parse_statements(text, dialect)
    reads: list[str] = []
    writes: list[str] = []
    for stmt in statements:
        for src in stmt.sources:
            if src not in reads:
                reads.append(src)
        # 只认真正的数据写入（insert / ctas）。纯 DDL 不算"写这张表"——它只定义表，
        # 算进去会让每个下游表的写者清单里都混进一份 ddl/*.sql，噪音盖过真正的落点。
        if stmt.kind in ("insert", "ctas") and stmt.target and stmt.target not in writes:
            writes.append(stmt.target)
    return FileInfo(
        path=path,
        abs_path=Path(path),
        statements=tuple(statements),
        reads=frozenset(reads),
        writes=frozenset(writes),
        error=error,
    )
