"""三个 SQL 工具的集成测试（走真实 registry + 真实 gate）。

分两部分：
- 用 tmp_path 现造的小数仓，验影响面端到端（坑二的端到端表现就在这里）；
- 用随仓库分发的 examples/dw_demo，验干净流水线零误报 + 每个埋点各命中一次，
  顺带保证那份演示素材本身不会腐坏。
"""

from pathlib import Path

import pytest

from emberpy import sql
from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.skills import SkillStore
from emberpy.tools import ToolCategory, ToolEnv, default_registry
from emberpy.tools.agent_tool import AGENT_TYPE_EXPLORE, build_subagent_registry

pytestmark = pytest.mark.skipif(
    not sql.SQLGLOT_AVAILABLE, reason="需要可选依赖 sqlglot（pip install emberpy[sql]）"
)

DEMO = Path(__file__).resolve().parents[3] / "examples" / "dw_demo"
SQL_TOOLS = ("sql_lineage", "sql_impact", "sql_lint")


def _env(workspace: Path, **extra: object) -> ToolEnv:
    return ToolEnv(
        workspace=workspace,
        gate=PermissionGate(PermissionMode("auto"), workspace),
        patches=PatchStore(),
        **extra,
    )


def _issues(out: str) -> str:
    """只要问题清单，切掉底部的「可用规则」图例。

    图例把全部 rule_id 都列一遍，不切掉的话 `rule_id not in out` 永远为假——
    测的是图例而不是规则有没有触发。图例只在零问题或用 ignore 时出现，这里统一切。
    """
    return out.split("可用规则")[0]


@pytest.fixture(autouse=True)
def _clean_index_cache():
    sql.reset_index_cache()
    yield
    sql.reset_index_cache()


@pytest.fixture()
def three_layers(tmp_path: Path) -> Path:
    """三层链路。故意每层都写别名——表名被别名污染时影响面会只查出一层就停。"""
    (tmp_path / "a.sql").write_text("INSERT OVERWRITE TABLE dws.t1 SELECT a.id FROM ods.t0 a;")
    (tmp_path / "b.sql").write_text("INSERT OVERWRITE TABLE dws.t2 SELECT b.id FROM dws.t1 b;")
    (tmp_path / "c.sql").write_text("INSERT OVERWRITE TABLE ads.t3 SELECT c.id FROM dws.t2 c;")
    return tmp_path


class TestRegistration:
    def test_three_tools_registered_and_read_only(self, tmp_path: Path) -> None:
        reg = default_registry(_env(tmp_path))
        for name in SQL_TOOLS:
            tool = reg.get(name)
            assert tool is not None, name
            # 只读不只是语义：explore 子 agent 与 plan 模式按 category 过滤，
            # 标错就会把写权限漏进只读池
            assert tool.category is ToolCategory.READ, name

    def test_explore_subagent_gets_sql_tools(self, tmp_path: Path) -> None:
        """explore 子 agent 与 plan 模式按 category 自动拿到，无需登记。"""
        base = default_registry(_env(tmp_path))
        explore = build_subagent_registry(base, AGENT_TYPE_EXPLORE).names()
        for name in SQL_TOOLS:
            assert name in explore, name
        # 确认只读过滤本身没被破坏
        assert "write_file" not in explore and "run_command" not in explore

    def test_missing_sqlglot_still_mounts_with_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺库时不能"不挂载"——那等于功能静默不存在，用户毫无察觉。"""
        from emberpy.tools import sql_tools

        monkeypatch.setattr(sql_tools, "SQLGLOT_AVAILABLE", False)
        tools = sql_tools.build_sql_tools(_env(tmp_path))
        assert [t.name for t in tools] == list(SQL_TOOLS)
        for tool in tools:
            out = tool.fn()
            assert out.startswith("错误：")
            assert "pip install sqlglot" in out


class TestSqlImpact:
    def test_three_layers_all_appear(self, three_layers: Path) -> None:
        reg = default_registry(_env(three_layers))
        out = reg.get("sql_impact").fn(table="ods.t0")
        assert "3 层" in out
        for table in ("dws.t1", "dws.t2", "ads.t3"):
            assert table in out, table
        # 层序不能乱：直接下游在第 1 层
        first = out.index("第 1 层：")
        assert out.index("dws.t1", first) < out.index("dws.t2", first) < out.index("ads.t3", first)

    def test_unqualified_name_is_resolved_with_a_note(self, three_layers: Path) -> None:
        reg = default_registry(_env(three_layers))
        out = reg.get("sql_impact").fn(table="t1")
        assert "后缀匹配" in out and "dws.t1" in out

    def test_leaf_table_reports_no_downstream(self, three_layers: Path) -> None:
        reg = default_registry(_env(three_layers))
        out = reg.get("sql_impact").fn(table="ads.t3")
        assert "没有查到下游" in out

    def test_unknown_table_is_explicit(self, three_layers: Path) -> None:
        reg = default_registry(_env(three_layers))
        out = reg.get("sql_impact").fn(table="no.such")
        assert "索引里没有这张表" in out
        assert "错误：" not in out  # 查不到不是错误，是有效结论

    def test_column_chain_on_demo_warehouse(self) -> None:
        """字段级要一路追到 ADS——改 ODS 的金额，业务看见的是 ADS 里的字段。"""
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_impact").fn(table="ods.ods_order_detail.amount")
        assert "dwd.dwd_order_detail.amount" in out
        assert "dws.dws_user_gmv_d.gmv" in out
        assert "ads.ads_user_report.gmv" in out
        assert "ads.ads_gmv_dashboard.total_gmv" in out

    def test_table_name_and_column_are_interchangeable(self) -> None:
        reg = default_registry(_env(DEMO))
        inline = reg.get("sql_impact").fn(table="dws.dws_user_gmv_d.gmv")
        separate = reg.get("sql_impact").fn(table="dws.dws_user_gmv_d", column="gmv")
        assert inline == separate

    def test_impact_lists_readers_and_writers(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_impact").fn(table="dwd.dwd_order_detail")
        assert "jobs/dwd_order_detail.sql" in out       # 写它的
        assert "jobs/dws_user_gmv_d.sql" in out         # 读它的
        assert "分区列：dt" in out
        # 纯 DDL 不算"写这张表"，否则每个下游的写者清单都会混进 ddl/*.sql
        assert "ddl/dwd.sql" not in out


class TestSqlLint:
    def test_clean_pipeline_has_zero_false_positives(self) -> None:
        """真实 CTE / 窗口函数 / UNION ALL / LEFT JOIN 上一条都不该报。

        这条是这套规则集的底线：能报出问题不难，难的是不误报。
        """
        (demo,) = (DEMO,)
        reg = default_registry(_env(demo))
        for path in sorted((demo / "jobs").glob("*.sql")):
            rel = path.relative_to(demo).as_posix()
            out = reg.get("sql_lint").fn(path=rel)
            assert "没有发现问题" in out, f"{rel} 出现误报：\n{out}"

    @pytest.mark.parametrize(
        ("path", "rule_id"),
        [
            ("problems/01_ops_daily_full.sql", "missing-partition-filter"),
            ("problems/02_user_sku_cross.sql", "cartesian-join"),
            ("problems/02_user_sku_cross.sql", "like-leading-wildcard"),
            ("problems/03_monthly_summary.sql", "partition-column-wrapped"),
            ("problems/04_gmv_full_overwrite.sql", "overwrite-without-partition"),
            ("problems/05_holiday_promo.sql", "hardcoded-date"),
            ("problems/06_sku_sales_incr.sql", "self-read-write"),
            ("problems/06_sku_sales_incr.sql", "select-star"),
        ],
    )
    def test_planted_problems_are_caught(self, path: str, rule_id: str) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lint").fn(path=path)
        assert rule_id in out, f"{path} 没报出 {rule_id}：\n{out}"

    def test_issues_carry_file_and_line(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lint").fn(path="problems/05_holiday_promo.sql")
        assert "problems/05_holiday_promo.sql:" in out

    def test_ignore_silences_a_rule(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lint").fn(path="problems/06_sku_sales_incr.sql", ignore=["select-star"])
        assert "select-star" not in _issues(out)
        assert "self-read-write" in _issues(out)

    def test_ddl_dependent_rules_need_the_workspace(self, tmp_path: Path) -> None:
        """没有 DDL 就不许猜：同一个文件在有/无建表语句的工作区结果不同。"""
        (tmp_path / "job.sql").write_text(
            "INSERT OVERWRITE TABLE dws.t SELECT a.id, a.dt FROM ods.a a WHERE a.dt='20240101';"
        )
        reg = default_registry(_env(tmp_path))
        assert "overwrite-without-partition" not in _issues(reg.get("sql_lint").fn(path="job.sql"))

        (tmp_path / "ddl.sql").write_text(
            "CREATE TABLE IF NOT EXISTS dws.t (id string, dt string) PARTITIONED BY (dt string);"
        )
        sql.reset_index_cache()
        assert "overwrite-without-partition" in _issues(reg.get("sql_lint").fn(path="job.sql"))

    def test_inline_sql_parse_error_is_an_error(self) -> None:
        reg = default_registry(_env(DEMO))
        assert reg.get("sql_lint").fn(sql="SELECT FROM WHERE (((").startswith("错误：")

    def test_rule_legend_only_shows_when_it_helps(self) -> None:
        """图例的取舍：零问题时给（把"没发现问题"变成"这几条都查过"），
        查出问题时不给（模型按文件调，8 行图例会重复到把问题挤出视野）。
        """
        reg = default_registry(_env(DEMO))
        clean = reg.get("sql_lint").fn(path="jobs/dws_user_gmv_d.sql")
        assert "没有发现问题" in clean and "可用规则" in clean

        dirty = reg.get("sql_lint").fn(path="problems/05_holiday_promo.sql")
        assert "hardcoded-date" in dirty and "可用规则" not in dirty

        # 正在用 ignore 管规则时要把清单给回来，否则没法知道还能关哪些
        silenced = reg.get("sql_lint").fn(path="problems/05_holiday_promo.sql", ignore=["hardcoded-date"])
        assert "本次已忽略：hardcoded-date" in silenced and "可用规则" in silenced


class TestSqlLineage:
    def test_table_level(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lineage").fn(path="jobs/dwd_order_detail.sql")
        assert "ods.ods_order_detail" in out and "ods.ods_order" in out
        assert "dwd.dwd_order_detail" in out
        # 默认不展开字段级：只有一个提示，没有逐列的箭头行
        assert "（要看字段级血缘，加 level=column）" in out
        assert "←" not in out

    def test_column_level_penetrates_cte(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lineage").fn(path="jobs/dws_order_summary_d.sql", level="column")
        assert "dwd.dwd_order_detail.amount" in out
        assert "dwd.dwd_payment.pay_amount" in out
        # CTE 名不能作为"表"出现
        assert "order_agg" not in out and "pay_agg" not in out

    def test_inline_sql(self) -> None:
        reg = default_registry(_env(DEMO))
        out = reg.get("sql_lineage").fn(
            sql="INSERT OVERWRITE TABLE ads.t PARTITION (dt) SELECT o.uid, SUM(o.amt) AS amt "
            "FROM dwd.src o WHERE o.dt = $[yyyyMMdd] GROUP BY o.uid;",
            level="column",
        )
        assert "dwd.src.amt" in out and "ads.t.amt" in out


_PATH_TOOLS = ("sql_lineage", "sql_lint")


class TestErrorContract:
    @pytest.mark.parametrize("name", _PATH_TOOLS)
    @pytest.mark.parametrize("kwargs", [{}, {"path": "nope.sql"}, {"path": "jobs"}])
    def test_path_tools_error_with_the_prefix(self, name: str, kwargs: dict) -> None:
        """错误必须带 错误： 前缀，否则界面不标 isError。

        三种输入都要有兜底：没传参数、文件不存在、指到目录上。
        """
        reg = default_registry(_env(DEMO))
        out = reg.get(name).fn(**kwargs)
        assert out.startswith("错误："), f"{name} 的返回没有错误前缀：{out[:60]}"

    def test_impact_missing_table_is_an_error_but_unknown_table_is_not(self) -> None:
        """漏传参数要拿到 错误：而不是 TypeError；但"查不到"是有效结论，不是错误。

        这个区分很重要：把"没查到下游"标成红色错误，模型会以为工具坏了去重试；
        标成正常结论，它才会当成事实用。
        """
        reg = default_registry(_env(DEMO))
        assert reg.get("sql_impact").fn().startswith("错误：")
        unknown = reg.get("sql_impact").fn(table="no.such")
        assert not unknown.startswith("错误：")
        assert "索引里没有这张表" in unknown
        assert "没有查到下游" not in unknown  # 表和"表没有下游"是两回事，不能混

    def test_impact_missing_table_returns_error_not_typeerror(self) -> None:
        """模型漏传必填参数时要拿到 错误：，而不是 TypeError 冒到引擎兜底。"""
        reg = default_registry(_env(DEMO))
        assert reg.get("sql_impact").fn().startswith("错误：")


class TestConditionalSkill:
    def test_skill_activates_on_touching_a_sql_file(self) -> None:
        """条件技能就是"自动影响面集成"的落地方式：读到 .sql 就激活，下一步模型可见。

        这个保证是免费的——写前必读护栏保证改任何已存在的 SQL 前必然读过它，
        所以技能必然已激活。
        """
        store = SkillStore.from_workspace(DEMO)
        assert "dw-sql-guard" in [s.name for s in store.conditional()]
        names_before = [s.name for s in store.available()]
        assert "dw-sql-guard" not in names_before, "碰之前不该可见"

        env = _env(DEMO, skills=store)
        reg = default_registry(env)
        out = reg.get("read_file").fn(path="jobs/dws_user_gmv_d.sql")
        assert "INSERT OVERWRITE" in out
        assert store.is_active("dw-sql-guard")
        assert "dw-sql-guard" in [s.name for s in store.available()]
        # 技能正文要真的命中"先查影响面"这件事，否则激活了也没用
        skill = store.get("dw-sql-guard")
        assert skill is not None
        assert "sql_impact" in skill.body and "sql_lint" in skill.body

    def test_non_sql_touch_does_not_activate(self) -> None:
        store = SkillStore.from_workspace(DEMO)
        env = _env(DEMO, skills=store)
        reg = default_registry(env)
        reg.get("read_file").fn(path="README.md")
        assert not store.is_active("dw-sql-guard")
