"""SQL 血缘 / 影响面 / 规范检查三个只读工具（数仓方向）。

解决的问题：数仓里改一个字段或改一次口径，下游影响面无法在动手前确定——血缘只
存在于人的脑子里或一份必然过期的文档里。agent 在改数仓代码之前本来就需要知道
这一改会波及谁，所以这是它进数仓项目时缺的那块上下文，不是外挂功能。

三个工具刻意不合并，它们回答三个不同的问题：
- sql_lineage  这段 SQL / 这个文件读写了谁（往下游看是"我依赖谁"）
- sql_impact   改这张表 / 这个字段，会波及谁（核心；反向）
- sql_lint     这段 SQL 有哪些数仓规范 / 性能问题

全部 ToolCategory.READ，于是 explore 子 agent 与 plan 模式**自动可用**，无需登记
（tools/agent_tool.py 按 category 过滤）；审查子 agent 本来就无写权限，这几个工具
正好是它能用的。

依赖 sqlglot 是可选的（见 emberpy/sql/__init__.py 的说明）。没装时三个工具照常
注册，调用时返回引导错误——不返回空列表，否则等于功能静默不存在、用户毫无察觉。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..sql import (
    DEFAULT_DIALECT,
    MISSING_SQLGLOT_HINT,
    RULE_SUMMARY,
    SQLGLOT_AVAILABLE,
    ColumnEdge,
    LintIssue,
    analyze_text,
    column_edges,
    get_index,
    lint_statements,
)
from .registry import Tool, ToolCategory, ToolEnv

# 工具结果循环层不截断（见 agent/core.py），自己留头留尾
_MAX_OUTPUT = 60_000
_MAX_LISTED_TABLES = 40
_MAX_LISTED_FILES = 25


def _capped(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n……[中间省略 {len(text) - limit} 字符]……\n{tail}"


def _short_list(items: list[str], limit: int = _MAX_LISTED_TABLES) -> list[str]:
    """列表过长时留头去尾并标注省略了多少——绝不静默截断。"""
    if len(items) <= limit:
        return items
    return [*items[:limit], f"…（另有 {len(items) - limit} 项未列出）"]


def _writers_suffix(source_files: list[str]) -> str:
    """下游表是被哪个文件写出来的。多写者要全列，只显示第一个会漏掉改动的落点。"""
    if not source_files:
        return ""
    if len(source_files) <= 3:
        return f"← {'、'.join(source_files)}"
    return f"← {source_files[0]} 等 {len(source_files)} 个文件"


def _format_layer(
    entries: list[tuple[str, list[str]]], label: str
) -> list[str]:
    """把一层的 (名字, 写它的文件) 对齐着列出来。

    对齐用层内最长名字的宽度算，不做全局对齐——全局对齐会让长表名把每一行都推得很远。
    """
    width = max((len(name) for name, _ in entries), default=0)
    lines = [f"  {label}"]
    for name, source_files in entries:
        suffix = _writers_suffix(source_files)
        lines.append(f"    - {name:<{width}}  {suffix}".rstrip())
    return lines


def _notes_block(index: Any) -> list[str]:
    """索引自身的告警（不完整 / 解析失败）必须回给模型。

    否则模型会把"没查到下游"当成"没有下游"，那是这个功能最危险的静默错误。
    """
    notes = list(getattr(index, "notes", ()) or ())
    if not notes:
        return []
    return ["", "注意：", *(f"  ! {note}" for note in notes)]


# ---------------------------------------------------------------------------
# sql_lineage
# ---------------------------------------------------------------------------


def _resolve_source(
    env: ToolEnv, path: Optional[str], sql: Optional[str]
) -> tuple[Optional[Any], Optional[str], Optional[str]]:
    """把 (path | sql) 归一成 (可分析的文本, 显示名, relative 路径)。

    返回 (None, None, 错误文本) 表示参数不对。
    """
    if sql and sql.strip():
        return (sql, "<内联 SQL>", None)
    if not path or not str(path).strip():
        return (None, None, "错误：需要给 path（工作区内的 .sql 文件）或 sql（一段 SQL 文本）之一。")
    target = Path(str(path))
    if not target.is_absolute():
        target = env.workspace / target
    if not target.exists():
        return (None, None, f"错误：文件不存在：{path}")
    if target.is_dir():
        return (None, None, f"错误：{path} 是目录，sql_lineage 只接受单个 .sql 文件。")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return (None, None, f"错误：读取 {path} 失败：{exc}")
    try:
        rel = target.resolve().relative_to(env.workspace.resolve()).as_posix()
    except ValueError:
        rel = target.as_posix()
    return (text, rel, None)


def _format_statements(info: Any) -> list[str]:
    lines: list[str] = []
    if info.error:
        lines.append(f"  解析失败：{info.error}")
        lines.append("  （该文件的血缘不纳入后续分析）")
        return lines
    if not info.statements:
        lines.append("  没有解析出任何语句（空文件或全是注释）")
        return lines
    for index, stmt in enumerate(info.statements, start=1):
        what = {
            "insert": "写入",
            "ctas": "建立/写入",
            "query": "查询",
            "ddl": "定义",
        }.get(stmt.kind, "其它")
        if stmt.target:
            head = f"  [{index}] 第 {stmt.line} 行 {what} {stmt.target}"
            if stmt.overwrite:
                head += "（INSERT OVERWRITE）"
            if stmt.kind == "insert" and not stmt.has_partition_clause:
                head += "（未写 PARTITION）"
        else:
            head = f"  [{index}] 第 {stmt.line} 行 {what}（无写入目标）"
        lines.append(head)
        sources = [s for s in stmt.sources if s != stmt.target]
        if sources:
            lines.append(f"        读取：{'、'.join(_short_list(sources))}")
        else:
            lines.append("        读取：（无）")
    return lines


def _format_column_edges(edges: list[ColumnEdge], info: Any) -> list[str]:
    if info.error:
        return []
    lines = ["", "字段级血缘（目标列 ← 源列）："]
    if not edges:
        lines.append("  （没有解析出字段级血缘：可能是纯 DDL、或全是常量/聚合到单值的写法）")
        return lines
    by_target: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        by_target.setdefault((edge.target_table, edge.target_column), []).append(
            f"{edge.source_table}.{edge.source_column}"
        )
    for (table, column), sources in sorted(by_target.items()):
        lines.append(f"  {table}.{column}")
        for source in _short_list(sorted(set(sources))):
            lines.append(f"      ← {source}")
    return lines


def build_sql_lineage_tool(env: ToolEnv) -> Tool:
    def sql_lineage(
        path: Optional[str] = None,
        sql: Optional[str] = None,
        level: str = "table",
        dialect: str = DEFAULT_DIALECT,
    ) -> str:
        text, name, error = _resolve_source(env, path, sql)
        if error is not None:
            return error
        assert text is not None
        info = analyze_text(text, dialect or DEFAULT_DIALECT, name or "<内联 SQL>")

        lines = [f"血缘：{name}", "", "语句与读写："]
        lines.extend(_format_statements(info))
        if str(level).lower() == "column" and not info.error:
            lines.extend(_format_column_edges(column_edges(text, dialect or DEFAULT_DIALECT), info))
        elif not info.error:
            lines.append("")
            lines.append("（要看字段级血缘，加 level=column）")
        return _capped("\n".join(lines))

    return Tool(
        name="sql_lineage",
        description=(
            "解析一个 .sql 文件或一段 SQL，列出它读了哪些表、写了哪些表（Hive/Spark 语法为主）。"
            "level=column 时进一步给出字段级血缘（目标列 ← 源列），能追到 "
            "SUM(oi.amount) AS gmv 里的 amount。用在：改一段 SQL 前先弄清它的上下游、"
            "追某个指标字段是从哪来的、读陌生的数仓脚本。反向的「改这张表会波及谁」用 sql_impact。"
            "多语句脚本（建表 + 插入）能整段解析；调度模板变量 ${bizdate} / $[yyyyMMdd] / {{ ds }} "
            "会先被替换成占位符再解析。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的 .sql 文件路径，与 sql 二选一",
                },
                "sql": {
                    "type": "string",
                    "description": "直接给一段 SQL 文本（还没落盘或只想试算），与 path 二选一",
                },
                "level": {
                    "type": "string",
                    "enum": ["table", "column"],
                    "description": "table=只看表级（默认，快）；column=额外算字段级血缘（慢一些）",
                    "default": "table",
                },
                "dialect": {
                    "type": "string",
                    "description": f"SQL 方言，默认 {DEFAULT_DIALECT}；可选 spark / presto / mysql 等",
                    "default": DEFAULT_DIALECT,
                },
            },
        },
        category=ToolCategory.READ,
        fn=sql_lineage,
    )


# ---------------------------------------------------------------------------
# sql_impact（核心）
# ---------------------------------------------------------------------------


def _split_target(raw: str) -> tuple[str, Optional[str]]:
    """`库.表` / `库.表.字段` -> (表名, 字段名)。

    三段的时候最后一段是字段。带库不带库都可以，后缀匹配交给 match_tables。
    """
    cleaned = raw.strip().strip('"`[]')
    parts = [p for p in cleaned.split(".") if p]
    if len(parts) >= 3:
        return (".".join(parts[:-1]), parts[-1])
    return (cleaned, None)


def build_sql_impact_tool(env: ToolEnv) -> Tool:
    def sql_impact(
        table: Optional[str] = None,
        column: Optional[str] = None,
        depth: int = 12,
        dialect: str = DEFAULT_DIALECT,
    ) -> str:
        # table 给 Optional 默认值而不是必填位置参数：模型漏传参数时这里能返回
        # "错误：..."，而不是抛 TypeError 冒到 agent/core.py 的兜底（措辞不可控）。
        if not table or not str(table).strip():
            return "错误：需要给 table，形如 dws.dws_user_gmv 或 dws.dws_user_gmv.gmv（库名可省）。"

        # 支持把字段直接写在 table 里（dws.t.col），也支持分开给
        table_name_part, inline_column = _split_target(str(table))
        column = (column or inline_column or "").strip() or None

        try:
            max_depth = max(1, min(int(depth), 12))
        except (TypeError, ValueError):
            max_depth = 12

        index = get_index(env.workspace, getattr(env, "gate", None), dialect or DEFAULT_DIALECT)
        matched, how = index.match_tables(table_name_part)
        lines: list[str] = []

        if not matched:
            lines.append(f"影响面：{table_name_part}")
            lines.append("")
            lines.append(f"  索引里没有这张表（已扫描 {len(index.files)} 个 SQL 文件）。")
            lines.append("  可能：表不由工作区里的 SQL 定义或写入（外部导入、BI 直连、或还没建）。")
            lines.append("  可以用 sql_lineage 看某个文件具体读写了哪些表，核对表名写法是否一致。")
            lines.extend(_notes_block(index))
            return _capped("\n".join(lines))

        if how:
            lines.append(f"（{how}）")
        target = matched[0]

        if column:
            layers = index.downstream_column_chain(target, column, max_depth)
            total = sum(len(layer) for layer in layers)
            lines.insert(0, f"字段影响面：{target}.{column} —— 下游 {len(layers)} 层、{total} 个字段")
            lines.append("")
            if not layers:
                lines.append("  （没有查到依赖这个字段的下游字段）")
                lines.append("  可能：该字段只是被原样搬运（未落进别的表）、或下游表的定义不在工作区。")
            else:
                for level, layer in enumerate(layers, start=1):
                    lines.extend(
                        _format_layer(
                            [(f"{tbl}.{col}", index.files_writing(tbl)) for tbl, col in layer],
                            f"第 {level} 层：",
                        )
                    )
            lines.extend(_notes_block(index))
            return _capped("\n".join(lines))

        layers = index.downstream(target, max_depth)
        total = sum(len(layer) for layer in layers)
        writers = index.files_writing(target)
        readers = index.files_reading(target)

        lines.insert(0, f"影响面：改 {target} 会波及下游 {len(layers)} 层、{total} 张表")
        lines.append("")
        if not layers:
            lines.append("  没有查到下游 —— 当前工作区里没有 SQL 读它。")
            lines.append("  （若是 ADS 层表直接给 BI 用，那确实是这样；若不是，先看下面的注意。）")
        else:
            for level, layer in enumerate(layers, start=1):
                lines.extend(
                    _format_layer(
                        [(tbl, index.files_writing(tbl)) for tbl in layer],
                        f"第 {level} 层：",
                    )
                )

        lines.append("")
        lines.append(f"写这张表的文件（{len(writers)}）：")
        lines.extend(f"  {p}" for p in _short_list(writers, _MAX_LISTED_FILES))
        if not writers:
            lines.append("  （无）")
        lines.append(f"读这张表的文件（{len(readers)}）：")
        lines.extend(f"  {p}" for p in _short_list(readers, _MAX_LISTED_FILES))
        if not readers:
            lines.append("  （无）")

        if index.catalog.get(target) is not None:
            table_def = index.catalog[target]
            if table_def.partition_columns:
                lines.append("")
                lines.append(f"分区列：{'、'.join(table_def.partition_columns)}")

        lines.append("")
        lines.append("（要精确到字段，加 column 参数，或写成 库.表.字段）")
        lines.extend(_notes_block(index))
        return _capped("\n".join(lines))

    return Tool(
        name="sql_impact",
        description=(
            "反向影响面：改这张表（或这个字段）会波及哪些下游表。数仓里最贵的一类事故是"
            "\"改一个字段、下游炸一片、而且动手前没人知道\"，改 SQL 之前应该先跑这个。"
            "按层级列出下游，并给出写它/读它的文件。给 column 参数（或写成 库.表.字段）时"
            "走字段级影响面，逐层接力：amount -> dws.gmv -> ads.total_gmv。"
            "库名可省，会按表名后缀匹配并注明。表级查询是毫秒级的，不依赖字段级血缘。"
            "输出里若出现「索引不完整」或「解析失败」，说明覆盖不全，别把「没查到下游」"
            "当成「没有下游」。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "表名，形如 dws.dws_user_gmv（库名可省），或 库.表.字段",
                },
                "column": {
                    "type": "string",
                    "description": "字段名。给了就走字段级影响面；也可以直接写在 table 里",
                },
                "depth": {
                    "type": "integer",
                    "description": "向下游展开的最大层数（1-12，默认 12）",
                    "default": 12,
                },
                "dialect": {
                    "type": "string",
                    "description": f"SQL 方言，默认 {DEFAULT_DIALECT}",
                    "default": DEFAULT_DIALECT,
                },
            },
            "required": ["table"],
        },
        category=ToolCategory.READ,
        fn=sql_impact,
    )


# ---------------------------------------------------------------------------
# sql_lint
# ---------------------------------------------------------------------------

_SEVERITY_LABEL = {
    "error": "错误",
    "warning": "警告",
    "info": "提示",
}


def _format_issues(issues: list[LintIssue], name: str, ignored: list[str]) -> str:
    lines = [f"SQL 规范检查：{name}", ""]
    if not issues:
        lines.append("  没有发现问题。")
    else:
        counts: dict[str, int] = {}
        for issue in issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        summary = "、".join(
            f"{_SEVERITY_LABEL.get(sev, sev)} {counts[sev]}"
            for sev in ("error", "warning", "info")
            if counts.get(sev)
        )
        lines.append(f"  共 {len(issues)} 条：{summary}")
        lines.append("")
        for severity in ("error", "warning", "info"):
            group = [i for i in issues if i.severity == severity]
            if not group:
                continue
            lines.append(f"  [{_SEVERITY_LABEL.get(severity, severity)}]")
            for issue in group:
                location = f"{issue.file}:{issue.line}" if issue.line else issue.file
                lines.append(f"    {location}  {issue.message}  ({issue.rule_id})")
                if issue.detail:
                    lines.append(f"        {issue.detail}")
            lines.append("")

    # 规则图例只在"没查出问题"或"用户正在用 ignore 管规则"时给。
    # 查出问题时图例是纯噪音——模型按文件逐个调这个工具，8 行图例会重复到把真正的
    # 问题挤出视野；而图例的用途（让模型知道有哪些 rule_id 可关）在第一次干净检查时
    # 已经给过了。零问题时反而最该列全：它把"没有发现问题"从一句空话变成
    # "这 8 条我都查过"，这条结论才可信。
    if not issues or ignored:
        lines.append("  可用规则（ignore 参数可关闭）：")
        for rule_id, summary in RULE_SUMMARY.items():
            lines.append(f"    {rule_id}：{summary}")
        if ignored:
            lines.append("")
            lines.append(f"  本次已忽略：{'、'.join(ignored)}")
    return _capped("\n".join(lines))


def build_sql_lint_tool(env: ToolEnv) -> Tool:
    def sql_lint(
        path: Optional[str] = None,
        sql: Optional[str] = None,
        dialect: str = DEFAULT_DIALECT,
        ignore: Any = None,
    ) -> str:
        text, name, error = _resolve_source(env, path, sql)
        if error is not None:
            return error
        assert text is not None
        if isinstance(ignore, str):
            ignore = [ignore]
        ignored = [str(item).strip() for item in (ignore or []) if str(item).strip()]

        if path and not (sql and sql.strip()):
            # 文件走索引：能拿到同工作区的 DDL 目录，依赖 DDL 的两条规则才生效
            index = get_index(env.workspace, getattr(env, "gate", None), dialect or DEFAULT_DIALECT)
            info = index.files.get(name or "")
            if info is None:
                info = analyze_text(text, dialect or DEFAULT_DIALECT, name or "<内联 SQL>")
            issues = lint_statements(info, index.catalog, ignored)
            if not issues and info.error:
                return f"错误：{name} 解析失败：{info.error}"
        else:
            info = analyze_text(text, dialect or DEFAULT_DIALECT, name or "<内联 SQL>")
            if info.error:
                return f"错误：SQL 解析失败：{info.error}"
            issues = lint_statements(info, {}, ignored)

        return _format_issues(issues, name or "<内联 SQL>", ignored)

    return Tool(
        name="sql_lint",
        description=(
            "检查一段 SQL / 一个 .sql 文件的数仓规范与性能问题，返回带 rule_id 与行号的问题清单。"
            "覆盖：笛卡尔积、分区列被函数包裹（打掉分区裁剪，数仓最经典的性能事故）、"
            "LIKE 前置通配符、硬编码日期（调度重跑不换日期）、INSERT OVERWRITE 分区表却没写 "
            "PARTITION（覆盖全部分区，破坏性）、自读自写、缺分区过滤、多表 join 用 SELECT *。"
            "靠 DDL 才能判的两条规则（覆盖分区、缺分区过滤）只在工作区里有该表建表语句时生效，"
            "没有就不猜、静默跳过。误报多的规则（隐式类型转换、同表重复扫描、大小表 join 顺序）"
            "已刻意不做。项目觉得某条吵可以用 ignore 关掉。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的 .sql 文件路径，与 sql 二选一（走文件才能用上工作区 DDL）",
                },
                "sql": {
                    "type": "string",
                    "description": "直接给一段 SQL 文本，与 path 二选一",
                },
                "dialect": {
                    "type": "string",
                    "description": f"SQL 方言，默认 {DEFAULT_DIALECT}",
                    "default": DEFAULT_DIALECT,
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要关闭的 rule_id 列表，如 [\"select-star\"]",
                },
            },
        },
        category=ToolCategory.READ,
        fn=sql_lint,
    )


def build_sql_tools(env: ToolEnv) -> list[Tool]:
    """三个 SQL 工具。sqlglot 没装时也照常注册，调用时返回引导错误。"""
    if not SQLGLOT_AVAILABLE:
        return [_build_unavailable_tool(name) for name in ("sql_lineage", "sql_impact", "sql_lint")]
    return [
        build_sql_lineage_tool(env),
        build_sql_impact_tool(env),
        build_sql_lint_tool(env),
    ]


def _build_unavailable_tool(name: str) -> Tool:
    """缺库时保留同名工具，调用返回引导错误。

    这是本仓的先例（tools/vision.py 无 key 时返回清晰引导错误，工具常驻注册表）。
    反过来"缺库就不挂载"等于功能静默不存在：用户看到的是模型说"我没有这个能力"，
    而不是"你 pip install 一下就有"。
    """

    def unavailable(**_kwargs: Any) -> str:
        return MISSING_SQLGLOT_HINT

    descriptions = {
        "sql_lineage": "解析 SQL 文件的表级/字段级血缘（需要 sqlglot，当前未安装）",
        "sql_impact": "反向影响面：改这张表会波及谁（需要 sqlglot，当前未安装）",
        "sql_lint": "SQL 规范与性能检查（需要 sqlglot，当前未安装）",
    }
    return Tool(
        name=name,
        description=descriptions.get(name, "SQL 分析（需要 sqlglot，当前未安装）"),
        parameters={"type": "object", "properties": {}},
        category=ToolCategory.READ,
        fn=unavailable,
    )
