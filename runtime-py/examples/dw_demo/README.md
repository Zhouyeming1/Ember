# 样例数仓（SQL 血缘 / 影响面 / 规范检查的演示与测试素材）

一个四层数仓的迷你版：20 张表、22 个 SQL 文件，从 ODS 一路跑到 ADS。
既是 `runtime-py/tests/unit/sql/` 的测试 fixture，也是这两个功能的演示素材。

## 为什么要做这个

数仓里最贵的一类事故不是语法错误——那个会直接报错跑不起来。真正贵的是**静默的
口径变化**：改一个字段、下游炸一片，而且动手前没人知道；等业务发现数字不对，可能
已经错了一周。血缘如果只存在于老员工的脑子里或一份必然过期的文档里，这件事迟早
会发生。

对 coding agent 来说这不是外挂功能，而是它进数仓项目时本来就缺的那块上下文：
**agent 在改数仓代码之前，本来就需要知道这一改会波及谁。**

## 跑起来

```bash
cd runtime-py
python3 examples/dw_demo/demo.py
```

不需要启动桌面端，也不调模型——直接构造工具环境、调工具函数、打印模型会看到的原文。

在桌面端里则是另一条路径：`.claude/skills/dw-sql-guard/` 是一个带
`paths: ["**/*.sql"]` 的条件技能，**只要 agent 读了或碰了这个项目里的任何 .sql 文件，
技能就自动激活**，下一步它就能在自己的 Skill 工具清单里看到「改 SQL 前先查影响面」。
不需要改引擎一行代码。

## 数据流

```text
ODS（贴源，按 dt/hour 分区）
  ods_order  ods_order_detail  ods_user  ods_sku  ods_payment
        │            │              │         │          │
        ▼            ▼              ▼         ▼          ▼
DWD（清洗：脱敏 / 补状态 / 统一口径）
  dwd_order_detail  dwd_user_info  dwd_sku_info  dwd_payment
        │                  │              │            │
        │                  ▼              ▼            ▼
        │            DIM（一致性维度）
        │              dim_user       dim_sku
        │                  │              │
        ▼                  │              │
DWS（主题轻度汇总）         │              │
  dws_user_gmv_d ──────────┼──────────────┘
  dws_sku_sales_d ─────────┤
  dws_order_summary_d ─────┤
        │                  │
        ▼                  ▼
ADS（BI 直连 / 对业务可见）
  ads_user_report   ads_gmv_dashboard
```

字段级血缘能一路追到底，这是最有说服力的一条演示：

```text
ods.ods_order_detail.amount
  └─ dwd.dwd_order_detail.amount            （明细宽表）
       ├─ dws.dws_user_gmv_d.gmv            （SUM(amount)）
       │    └─ ads.ads_user_report.gmv      （用户报表，BI 直连）
       │    └─ ads.ads_gmv_dashboard.total_gmv（GMV 大盘）
       ├─ dws.dws_order_summary_d.order_amount
       └─ dws.dws_sku_sales_d.sales_amount
```

改 ODS 那个 `amount`，业务能看见的是 ADS 那两个字段——中间隔了三层。
这就是为什么"只查一层"不够：真正炸的是隔了一两层之后。

## `jobs/` 与 `problems/`

`jobs/` 是干净的生产脚本，**八条规则一条都不报**。这是刻意的：一个能报出问题的
linter 谁都能写，难的是在真实的 CTE、窗口函数、UNION ALL、LEFT JOIN、子查询、
`CASE WHEN`、`md5` 上**一条误报都没有**。干净流水线的零误报是这套规则集的底线。

`problems/` 每个文件埋一类问题，一条规则恰好命中一次：

| 文件 | 埋的问题 | 命中的 rule_id | 级别 |
| --- | --- | --- | --- |
| `01_ops_daily_full.sql` | 以为 `GROUP BY dt` 就等于按天取数，漏了分区过滤 | `missing-partition-filter` | 警告 |
| `02_user_sku_cross.sql` | 逗号连接漏关联键；`LIKE '%手机%'` | `cartesian-join` / `like-leading-wildcard` | 警告 / 提示 |
| `03_monthly_summary.sql` | `substr(dt,1,6)='202401'` 打掉分区裁剪 | `partition-column-wrapped` | 警告 |
| `04_gmv_full_overwrite.sql` | `INSERT OVERWRITE` 分区表却没写 `PARTITION` | `overwrite-without-partition` | **错误** |
| `05_holiday_promo.sql` | 分区过滤写死 `'2024-01-01'` | `hardcoded-date` | 警告 |
| `06_sku_sales_incr.sql` | 自读自写 + `SELECT *` | `self-read-write` / `select-star` | 警告 / 提示 |

`04` 是里面最危险的一个：看上去只是一次日更，实际会把**所有历史分区**覆盖掉，
第二天全公司的看板都会变。`03` 是最经典的性能事故：分区裁剪失效，全表扫描。

## 刻意不做的规则

误报会直接毁掉这个功能的可信度——模型看到满屏噪音就会开始忽略它，用户会关掉整个
工具。所以下面几条明知"看起来懂数仓"的规则是**主动不做**的：

- **隐式类型转换**：静态下没有类型信息。Hive 里 `where dt = 20240101` 这种字符串列
  比数字字面量极其常见且合法，catalog 一不全就满屏误报，而修复建议（让模型去加
  cast）往往是有害的。
- **同表重复扫描**：自连接（`from t a join t b` 比昨天今天）、`union all` 同表不同
  分区都是合法常见写法，判"重复"需要语义。
- **大小表 join 顺序**：需要真实行数统计，静态分析拿不到；Hive/Spark 里 join 顺序
  本来就由优化器决定。这是 MapReduce 时代的遗留规则。

靠建表语句才能判的两条（`overwrite-without-partition`、`missing-partition-filter`）
只在工作区里确实有该表 DDL 时生效——**没有就静默跳过，不猜**。

## 面试时可以这样讲（STAR）

- **S｜背景**：数仓里改字段/改口径的影响面无法在动手前确定，血缘靠人记和文档，
  而文档必然过期。下游要么全线报错，要么更糟——数字悄悄变了，等业务发现已经错了一周。
- **T｜任务**：让 agent 在改数仓代码之前就能说清"这一改会波及谁"，并且这个判断要能
  在真实项目上跑，不是玩具。
- **A｜做法**：用 sqlglot 建工作区级的表级 + 字段级血缘图；三个只读工具
  （血缘 / 反向影响面 / 规范检查）；反向影响面做成表级毫秒级、字段级逐层接力；
  规范检查刻意只保留静态可判、低误报的规则；用条件技能让 agent 碰 `.sql` 时自动
  想起查影响面，零引擎改动。
- **R｜结果**：20 张表、22 文件、四层链路的样例数仓上，字段级影响面能从
  `ods.ods_order_detail.amount` 一路追到 ADS 的 `ads_gmv_dashboard.total_gmv`（跨 3 层）；
  八条规则在干净流水线上零误报，在 6 个埋了问题的脚本上各命中一次。
