-- DWS 层：按天 + 主题聚合的轻度汇总。这一层是"改一个字段会不会炸一片"的重灾区：
-- DWS 的字段会被多个 ADS 报表复用，口径一动，下游全变。

CREATE TABLE IF NOT EXISTS dws.dws_user_gmv_d (
  user_id       string        COMMENT '用户id',
  gmv           decimal(18,2) COMMENT '成交金额（明细 amount 求和）',
  order_cnt     bigint        COMMENT '订单数（去重）',
  sku_cnt       bigint        COMMENT '购买商品数（去重）'
)
COMMENT '用户日粒度 GMV'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dws.dws_sku_sales_d (
  sku_id        string        COMMENT '商品id',
  category_id   string        COMMENT '类目id',
  sales_amount  decimal(18,2) COMMENT '销售额',
  sales_qty     bigint        COMMENT '销量',
  buyer_cnt     bigint        COMMENT '购买人数（去重）'
)
COMMENT '商品日粒度销量'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dws.dws_order_summary_d (
  order_id      string        COMMENT '订单id',
  user_id       string        COMMENT '用户id',
  order_amount  decimal(18,2) COMMENT '订单金额',
  pay_amount    decimal(18,2) COMMENT '实付金额（pay_paid 口径，不走退款）',
  pay_channel   string        COMMENT '支付渠道',
  order_status  int           COMMENT '订单状态'
)
COMMENT '订单日汇总（含支付信息）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;
