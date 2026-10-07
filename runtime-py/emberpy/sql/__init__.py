"""SQL 血缘与规范检查（数仓场景，Hive / Spark 语法为主）。

给 agent 用的能力：改一张表或一个字段之前，先知道这一改会波及哪些下游。数仓里
"改一个字段、下游炸一片、而且没人提前知道"是最贵的一类事故，血缘不该只存在于
老员工的脑子或一份必然过期的文档里。

本包只负责解析与建图，返回结构化结果；面向模型的文本由 tools/sql_tools.py 拼。

依赖 sqlglot（可选）：放 `[project.optional-dependencies] sql` 而不是硬依赖，因为
桌面端拿系统的 python3 跑引擎、没有内置虚拟环境（src/main/agent-host.ts 用
PYTHONPATH 指到 runtime-py），硬依赖会让没装的机器连引擎都起不来。未安装时
SQLGLOT_AVAILABLE 为 False，工具照常注册、调用时返回引导错误（同 tools/vision.py
无 key 时的做法）。

—— 六个已实测的坑，改这个包之前先读 ——

1. sqlglot.lineage() 遇到多语句 SQL 直接抛 SqlglotError（它内部走 parse_one，
   多语句会返回 Block 包装）。真实数仓 .sql 脚本几乎都是"建表 + 插入"多语句，
   所以必须先用 sqlglot.parse() 拿语句列表，逐条只对查询语句跑 lineage。
2. 表名绝不能用 tb.sql() 取：`FROM dws.t g` 会取成 `dws.t AS g`，别名混进去会让
   血缘图的边对不上——表现是影响面查询"只查出一层就停"，漏掉下游，且看起来正常。
   统一走 table_name()（结构化字段拼接 + 小写归一）。
3. sqlglot 会规范化 AST，别假设它和源码字面一致：`JOIN b`（无 ON）→ `JOIN b ON
   TRUE`；`FROM a, b` 与 `CROSS JOIN` → kind='CROSS'。判笛卡尔积必须两种都认。
4. 调度模板变量要先预替换：`WHERE dt = $[yyyyMMdd]`（DolphinScheduler 常见的不带
   引号写法）会直接抛 ParseError，而 `${bizdate}` / `{{ ds }}` 能解析。数仓 SQL 里
   这类变量遍地都是，漏掉这步工具在真实项目上基本不可用。
5. 给 lineage() 传不完整的 schema= 会让它抛 OptimizeError 而不是降级。所以不要把
   DDL 目录顺手喂给 lineage 当 schema。
6. lineage(column=None) 一次返回全部输出列，且跨列共享 cache。别按列循环调用，
   那是 N 倍的 qualify 开销。

另有一条省事的路：lineage(sources={...}) 是 sqlglot 自带的跨文件展开，且返回的
source_name 是干净的（没有别名污染）——但只展开 upstream，反向影响面仍需自建索引。

—— 包内分工 ——

base.py     可选依赖判定、常量、表名归一、共享数据类（唯一不依赖同包其它模块）
parse.py    模板变量预替换、多语句切分、表与分区定义提取
lineage.py  字段级血缘（目标列 <- 源列）
index.py    工作区索引：文件 -> 读写表 -> 正反向依赖图，含缓存与失效
rules.py    规范与性能规则
"""
from __future__ import annotations

from .base import (
    DEFAULT_DIALECT,
    MAX_COLUMN_LINEAGE_STATEMENTS,
    MAX_INDEX_FILES,
    MAX_IMPACT_DEPTH,
    MAX_SQL_FILE_BYTES,
    MISSING_SQLGLOT_HINT,
    PARSE_BUDGET_SECONDS,
    SCHED_VAR_PLACEHOLDER,
    SQL_SUFFIXES,
    SQLGLOT_AVAILABLE,
    FileInfo,
    StatementInfo,
    TableDef,
    preprocess_templates,
    split_table_name,
    table_name,
)
from .index import SqlIndex, get_index, reset_index_cache
from .lineage import ColumnEdge, column_edges
from .parse import analyze_text, parse_statements, parse_table_defs
from .rules import (
    RULE_SUMMARY,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    LintIssue,
    lint_statements,
)

__all__ = [
    # 底座
    "SQLGLOT_AVAILABLE",
    "MISSING_SQLGLOT_HINT",
    "DEFAULT_DIALECT",
    "SQL_SUFFIXES",
    "SCHED_VAR_PLACEHOLDER",
    "MAX_INDEX_FILES",
    "MAX_SQL_FILE_BYTES",
    "PARSE_BUDGET_SECONDS",
    "MAX_COLUMN_LINEAGE_STATEMENTS",
    "MAX_IMPACT_DEPTH",
    "preprocess_templates",
    "table_name",
    "split_table_name",
    "StatementInfo",
    "FileInfo",
    "TableDef",
    # 解析
    "analyze_text",
    "parse_statements",
    "parse_table_defs",
    # 血缘
    "ColumnEdge",
    "column_edges",
    # 索引
    "SqlIndex",
    "get_index",
    "reset_index_cache",
    # 规则
    "LintIssue",
    "lint_statements",
    "RULE_SUMMARY",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
]
