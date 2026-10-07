-- ADS 层：直接给 BI / 接口 / 报表用的表。改这一层的字段名或口径 =
-- 直接对业务可见的影响，动之前必须先查影响面。

CREATE TABLE IF NOT EXISTS ads.ads_user_report (
  user_id         string      COMMENT '用户id',
  user_name       string      COMMENT '用户名',
  user_level_name string      COMMENT '等级名',
  gmv             decimal(18,2) COMMENT '成交金额',
  order_cnt       bigint      COMMENT '订单数'
)
COMMENT '用户经营报表'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ads.ads_gmv_dashboard (
  stat_dim        string      COMMENT '统计维度：all / category',
  stat_key        string      COMMENT '维度值，all 时为 ALL',
  total_gmv       decimal(18,2) COMMENT 'GMV 合计',
  pay_gmv         decimal(18,2) COMMENT '实付 GMV',
  order_cnt       bigint      COMMENT '订单数',
  buyer_cnt       bigint      COMMENT '买家数'
)
COMMENT 'GMV 大盘（BI 看板直连，口径变更需同步业务）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

-- 下面两张是运维/活动用的报表，一并放 ADS
CREATE TABLE IF NOT EXISTS ads.ads_ops_daily_full (
  user_id       string        COMMENT '用户id',
  gmv           decimal(18,2) COMMENT '成交金额',
  dt            string        COMMENT '业务日期'
)
COMMENT '运营日全量报表'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ads.ads_holiday_promo (
  user_id       string        COMMENT '用户id',
  promo_gmv     decimal(18,2) COMMENT '活动期成交金额',
  dt            string        COMMENT '业务日期'
)
COMMENT '节日活动复盘'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ads.ads_user_sku_cross (
  user_id       string        COMMENT '用户id',
  sku_id        string        COMMENT '商品id',
  dt            string        COMMENT '业务日期'
)
COMMENT '用户-商品交叉表（试验）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ads.ads_monthly_summary (
  user_id       string        COMMENT '用户id',
  month_gmv     decimal(18,2) COMMENT '月累计成交金额',
  dt            string        COMMENT '业务日期'
)
COMMENT '用户月累计'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;
