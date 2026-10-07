"""emberpy/sql 的解析 / 建图 / 规则测试。

重点是把六条实测坑固定成回归测试，以及索引的增量与失效——这几处出错都不报异常，
只会在结果里安静地少一块，"看起来正常"恰恰是最危险的地方。
"""

from pathlib import Path

import pytest

from emberpy import sql

pytestmark = pytest.mark.skipif(
    not sql.SQLGLOT_AVAILABLE, reason="需要可选依赖 sqlglot（pip install emberpy[sql]）"
)


@pytest.fixture(autouse=True)
def _clean_index_cache():
    """模块级索引缓存跨用例共享，按目录键控；每个用例前后都清一次，避免串味。"""
    sql.reset_index_cache()
    yield
    sql.reset_index_cache()


class TestParsing:
    def test_multi_statement_file_parses(self) -> None:
        """坑一：lineage 对多语句直接抛错，必须先切分语句。

        真实数仓脚本几乎都是"建表 + 插入"，这条挂掉等于工具在真实项目上不可用。
        """
        text = """
        CREATE TABLE IF NOT EXISTS ads.t (uid string, amt decimal(18,2))
        PARTITIONED BY (dt string) STORED AS ORC;

        INSERT OVERWRITE TABLE ads.t PARTITION (dt)
        SELECT u.uid, SUM(u.amt) AS amt, u.dt
        FROM dws.src u WHERE u.dt = '20240101' GROUP BY u.uid, u.dt;
        """
        statements, error = sql.parse_statements(text)
        assert error is None
        assert len(statements) == 2
        assert statements[0].kind == "ddl"
        assert statements[1].kind == "insert"
        assert statements[1].target == "ads.t"
        assert statements[1].sources == ("dws.src",)
        # 多语句下字段级血缘也要能出来（坑一 + 坑六）
        assert sql.column_edges(text)

    def test_alias_does_not_pollute_table_name(self) -> None:
        """坑二：表名不能用 tb.sql() 取，否则 `dws.t g` 会变成 `dws.t AS g`。

        后果不是难看，是血缘图的边断裂——影响面只查出一层就停，且不报错。
        """
        info = sql.analyze_text("INSERT OVERWRITE TABLE dws.dst SELECT a.id FROM dws.src a;")
        assert info.reads == frozenset({"dws.src"})
        assert info.writes == frozenset({"dws.dst"})
        assert all(" AS " not in name and " " not in name for name in info.reads)
        # 三截限定名（catalog.db.table）也要归一
        assert sql.parse_statements("SELECT 1 FROM hive.dws.t;")[0][0].sources == ("hive.dws.t",)

    def test_scheduler_template_variables(self) -> None:
        """坑四：$[yyyyMMdd] 不带引号会抛 ParseError，必须先预替换。"""
        for raw in ("$[yyyyMMdd]", "${bizdate}", "{{ ds }}", "$bizdate"):
            text = f"INSERT OVERWRITE TABLE dws.dst SELECT a.id FROM dws.src a WHERE a.dt = {raw};"
            statements, error = sql.parse_statements(text)
            assert error is None, f"{raw} 解析失败：{error}"
            assert statements[0].sources == ("dws.src",)
        # 带引号的形式（'${bizdate}'）也不能被替换成两个引号套一层
        quoted = "INSERT OVERWRITE TABLE dws.dst SELECT a.id FROM dws.src a WHERE a.dt = '${bizdate}';"
        statements, error = sql.parse_statements(quoted)
        assert error is None
        assert statements[0].sources == ("dws.src",)

    def test_template_substitution_keeps_line_numbers(self) -> None:
        """替换不能改行数，否则 lint 报的行号全错位。"""
        text = "SELECT 1\nFROM dws.src\nWHERE dt = $[yyyyMMdd]\n"
        assert sql.preprocess_templates(text).count("\n") == text.count("\n")

    def test_parse_error_is_reported_not_swallowed(self) -> None:
        """解析失败必须显式记下来，不能当成"这个文件没有血缘"。"""
        info = sql.analyze_text("SELECT FROM WHERE (((")
        assert info.error
        assert info.statements == ()

    def test_partition_columns_from_ddl(self) -> None:
        """分区列是两条依赖 DDL 的规则的前提，Schema 包装那层容易取空。"""
        defs = sql.parse_table_defs(
            "CREATE TABLE IF NOT EXISTS ods.t (a string, b int) "
            "PARTITIONED BY (dt string, hour string) STORED AS ORC;"
        )
        assert len(defs) == 1
        assert defs[0].name == "ods.t"
        assert defs[0].columns == ("a", "b")
        assert defs[0].partition_columns == ("dt", "hour")
        assert defs[0].storage == "ORC"

    def test_plain_ddl_is_not_a_write(self) -> None:
        """纯 DDL 只定义表，不算"写这张表"——否则每个下游的写者清单都会混进 ddl/*.sql。"""
        info = sql.analyze_text("CREATE TABLE IF NOT EXISTS ods.t (a string) PARTITIONED BY (dt string);")
        assert info.writes == frozenset()
        assert info.reads == frozenset()

    def test_cte_names_are_not_tables(self) -> None:
        """CTE 引用在 AST 里也是 Table 节点，不排除会凭空造出 order_agg 这种假表。"""
        text = """
        INSERT OVERWRITE TABLE dws.dst PARTITION (dt)
        WITH order_agg AS (SELECT id, amt FROM dwd.src WHERE dt = '20240101')
        SELECT o.id FROM order_agg o WHERE o.dt = '20240101';
        """
        statements, error = sql.parse_statements(text)
        assert error is None
        assert statements[0].sources == ("dwd.src",)
        assert "order_agg" not in statements[0].sources
        # 带库名时是真表，不能因为同名 CTE 被误排除
        text2 = """
        INSERT OVERWRITE TABLE dws.dst PARTITION (dt)
        WITH t AS (SELECT 1 AS id)
        SELECT x.id FROM dws.t x WHERE x.dt = '20240101';
        """
        assert sql.parse_statements(text2)[0][0].sources == ("dws.t",)


class TestColumnLineage:
    def test_returns_all_columns_in_one_pass(self) -> None:
        """坑六：lineage(column=None) 一次返回全部输出列，别按列循环。"""
        text = """
        INSERT OVERWRITE TABLE dws.dst PARTITION (dt)
        SELECT o.user_id, SUM(o.amount) AS gmv, COUNT(DISTINCT o.order_id) AS cnt, o.dt
        FROM dwd.src o WHERE o.dt = '20240101' GROUP BY o.user_id, o.dt;
        """
        edges = sql.column_edges(text)
        pairs = {(e.target_column, e.source_column) for e in edges}
        assert ("gmv", "amount") in pairs
        assert ("user_id", "user_id") in pairs
        assert ("cnt", "order_id") in pairs
        assert all(e.source_table == "dwd.src" for e in edges)

    def test_lineage_ok_with_partial_catalog(self) -> None:
        """坑五：给 lineage 传不完整的 schema= 会抛 OptimizeError 而不是降级。

        所以实现里**从不**把 DDL 目录喂给 lineage。这里让工作区只覆盖一部分表的 DDL，
        确认字段级血缘照样能算——防止以后有人"顺手"把 catalog 当 schema 传进去。
        """
        text = "INSERT OVERWRITE TABLE dws.dst PARTITION (dt) SELECT o.amount FROM dwd.unknown o WHERE o.dt = '20240101';"
        edges = sql.column_edges(text)
        assert [(e.target_column, e.source_table, e.source_column) for e in edges] == [
            ("amount", "dwd.unknown", "amount")
        ]

    def test_column_chain_across_three_layers(self, tmp_path: Path) -> None:
        """字段级要逐层接力，只看一层会漏掉隔了一两层才炸的那种。"""
        (tmp_path / "a.sql").write_text(
            "INSERT OVERWRITE TABLE dwd.b PARTITION (dt) SELECT s.amount FROM ods.a s WHERE s.dt='20240101';"
        )
        (tmp_path / "b.sql").write_text(
            "INSERT OVERWRITE TABLE dws.c PARTITION (dt) SELECT SUM(b.amount) AS gmv FROM dwd.b b "
            "WHERE b.dt='20240101' GROUP BY b.dt;"
        )
        (tmp_path / "c.sql").write_text(
            "INSERT OVERWRITE TABLE ads.d PARTITION (dt) SELECT c.gmv FROM dws.c c WHERE c.dt='20240101';"
        )
        index = sql.get_index(tmp_path)
        chain = index.downstream_column_chain("ods.a", "amount")
        assert chain == [
            [("dwd.b", "amount")],
            [("dws.c", "gmv")],
            [("ads.d", "gmv")],
        ]


class TestIndex:
    def test_alias_chain_does_not_stop_early(self, tmp_path: Path) -> None:
        """坑二的端到端表现：表名被别名污染时，影响面会只查出一层就静默停下。"""
        (tmp_path / "a.sql").write_text("INSERT OVERWRITE TABLE dws.t1 SELECT a.id FROM ods.t0 a;")
        (tmp_path / "b.sql").write_text("INSERT OVERWRITE TABLE dws.t2 SELECT b.id FROM dws.t1 b;")
        (tmp_path / "c.sql").write_text("INSERT OVERWRITE TABLE ads.t3 SELECT c.id FROM dws.t2 c;")
        index = sql.get_index(tmp_path)
        assert index.downstream("ods.t0") == [["dws.t1"], ["dws.t2"], ["ads.t3"]]
        assert index.upstream("ads.t3") == [["dws.t2"], ["dws.t1"], ["ods.t0"]]

    def test_incremental_refresh_and_deletion(self, tmp_path: Path) -> None:
        target = tmp_path / "a.sql"
        target.write_text("INSERT OVERWRITE TABLE dws.x SELECT id FROM ods.y;")
        index = sql.get_index(tmp_path)
        assert index.downstream("ods.y") == [["dws.x"]]

        # 改内容后要重解析
        target.write_text("INSERT OVERWRITE TABLE dws.z SELECT id FROM ods.y;")
        index = sql.get_index(tmp_path)
        assert index.downstream("ods.y") == [["dws.z"]]

        # 删文件后旧边必须摘掉，否则图会越来越脏
        target.unlink()
        index = sql.get_index(tmp_path)
        assert index.downstream("ods.y") == []
        assert index.forward == {}

    def test_skips_scan_skip_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "ok.sql").write_text("INSERT OVERWRITE TABLE dws.x SELECT id FROM ods.y;")
        for junk in ("node_modules", ".git", "__pycache__"):
            d = tmp_path / junk
            d.mkdir()
            (d / "nope.sql").write_text("INSERT OVERWRITE TABLE dws.bad SELECT id FROM ods.bad;")
        index = sql.get_index(tmp_path)
        assert list(index.files) == ["ok.sql"]

    def test_match_tables_suffix_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "a.sql").write_text("INSERT OVERWRITE TABLE dws.orders SELECT id FROM ods.src;")
        index = sql.get_index(tmp_path)
        hits, how = index.match_tables("orders")
        assert hits == ["dws.orders"]
        assert how and "后缀" in how
        assert index.match_tables("dws.orders") == (["dws.orders"], None)
        assert index.match_tables("nope") == ([], None)


class TestRules:
    def _ids(self, text: str, **kwargs: object) -> list[str]:
        info = sql.analyze_text(text)
        return [issue.rule_id for issue in sql.lint_statements(info, kwargs.get("catalog") or {})]

    def test_cartesian_join_both_shapes(self) -> None:
        """坑三：`JOIN b`（无 ON）被规范化成 ON TRUE，逗号/CROSS 是 kind='CROSS'。

        只判 `on is None` 会让这条规则永不触发。
        """
        assert "cartesian-join" in self._ids("SELECT a.id FROM ods.a a JOIN ods.b b WHERE a.dt=1;")
        assert "cartesian-join" in self._ids("SELECT a.id FROM ods.a a, ods.b b WHERE a.dt=1;")
        assert "cartesian-join" in self._ids("SELECT a.id FROM ods.a a CROSS JOIN ods.b b;")
        # 正常 join 不能误报
        assert self._ids("SELECT a.id FROM ods.a a JOIN ods.b b ON a.id = b.id;") == []

    def test_self_read_write(self) -> None:
        assert "self-read-write" in self._ids("INSERT OVERWRITE TABLE dws.t SELECT id FROM dws.t;")

    def test_overwrite_without_partition_needs_ddl(self) -> None:
        """坑：Insert.args['partition'] 缺省是 False 而不是 None，判 None 会永不触发。"""
        text = "INSERT OVERWRITE TABLE dws.t SELECT a.id, a.dt FROM ods.a a WHERE a.dt='20240101';"
        catalog = {
            "dws.t": sql.TableDef(name="dws.t", partition_columns=("dt",)),
        }
        assert "overwrite-without-partition" in self._ids(text, catalog=catalog)
        # 没有 DDL 就不猜
        assert "overwrite-without-partition" not in self._ids(text)
        # 写了 PARTITION 就不该报
        with_partition = (
            "INSERT OVERWRITE TABLE dws.t PARTITION (dt) SELECT a.id, a.dt FROM ods.a a "
            "WHERE a.dt='20240101';"
        )
        assert "overwrite-without-partition" not in self._ids(with_partition, catalog=catalog)

    def test_partition_column_wrapped(self) -> None:
        text = "INSERT OVERWRITE TABLE dws.t PARTITION (dt) SELECT a.id FROM ods.a a WHERE substr(a.dt,1,6)='202401';"
        catalog = {"ods.a": sql.TableDef(name="ods.a", partition_columns=("dt",))}
        assert "partition-column-wrapped" in self._ids(text, catalog=catalog)
        # 非分区列被函数包裹是正常业务逻辑，不能报
        text2 = "SELECT a.id FROM ods.a a WHERE substr(a.sku_name,1,2)='手机';"
        assert "partition-column-wrapped" not in self._ids(text2, catalog=catalog)

    def test_hardcoded_date_not_flagged_for_template_variable(self) -> None:
        """用了调度变量就不算写死日期——这正是预替换换来的区分能力。"""
        catalog = {"ods.a": sql.TableDef(name="ods.a", partition_columns=("dt",))}
        assert "hardcoded-date" in self._ids(
            "SELECT a.id FROM ods.a a WHERE a.dt='2024-01-01';", catalog=catalog
        )
        assert "hardcoded-date" not in self._ids(
            "SELECT a.id FROM ods.a a WHERE a.dt=${bizdate};", catalog=catalog
        )

    def test_missing_partition_filter_needs_ddl(self) -> None:
        catalog = {"dws.t": sql.TableDef(name="dws.t", partition_columns=("dt",))}
        text = "INSERT OVERWRITE TABLE ads.o PARTITION (dt) SELECT t.uid, t.dt FROM dws.t t GROUP BY t.uid, t.dt;"
        assert "missing-partition-filter" in self._ids(text, catalog=catalog)
        filtered = (
            "INSERT OVERWRITE TABLE ads.o PARTITION (dt) SELECT t.uid, t.dt FROM dws.t t "
            "WHERE t.dt=${bizdate} GROUP BY t.uid, t.dt;"
        )
        assert "missing-partition-filter" not in self._ids(filtered, catalog=catalog)

    def test_ignore_closes_a_rule(self) -> None:
        info = sql.analyze_text("SELECT a.id FROM ods.a a JOIN ods.b b;")
        assert [i.rule_id for i in sql.lint_statements(info, {}, ["cartesian-join"])] == []

    def test_like_leading_wildcard_and_select_star(self) -> None:
        info = sql.analyze_text(
            "INSERT OVERWRITE TABLE dws.t PARTITION (dt) SELECT * FROM ods.a a JOIN ods.b b "
            "ON a.id=b.id WHERE a.dt='20240101' AND a.name LIKE '%x';"
        )
        ids = [i.rule_id for i in sql.lint_statements(info, {})]
        assert "like-leading-wildcard" in ids
        assert "select-star" in ids
        # 单表 select * 不是问题，不能报
        single = sql.analyze_text("SELECT * FROM ods.a WHERE dt='20240101';")
        assert "select-star" not in [i.rule_id for i in sql.lint_statements(single, {})]

    def test_issue_carries_line_number(self) -> None:
        """行号只挂在 token 级节点上，结构节点（Join / EQ）的 meta 是空的——直接读会全是 0。"""
        info = sql.analyze_text("SELECT a.id\nFROM ods.a a, ods.b b\nWHERE a.dt='20240101';")
        by_rule = {issue.rule_id: issue.line for issue in sql.lint_statements(info, {})}
        assert by_rule["cartesian-join"] == 2   # 结构节点（Join）要能下沉找到行号
        assert by_rule["hardcoded-date"] == 3   # 字面量节点自带行号
