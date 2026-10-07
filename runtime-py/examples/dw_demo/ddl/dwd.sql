-- DWD 层：清洗后的明细/维度，一般做三件事——脱敏、补维度、统一口径。
-- 全部按天分区，dt 是后续所有分区裁剪检查的依据。

CREATE TABLE IF NOT EXISTS dwd.dwd_order_detail (
  detail_id     string        COMMENT '明细id',
  order_id      string        COMMENT '订单id',
  user_id       string        COMMENT '用户id',
  sku_id        string        COMMENT '商品id',
  quantity      int           COMMENT '数量',
  amount        decimal(18,2) COMMENT '明细金额（元，已扣优惠）',
  discount      decimal(18,2) COMMENT '优惠金额',
  order_status  int           COMMENT '订单状态（补自主表）',
  create_time   string        COMMENT '下单时间'
)
COMMENT '订单明细宽表'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dwd.dwd_user_info (
  user_id        string       COMMENT '用户id',
  user_name      string       COMMENT '用户名',
  phone_md5      string       COMMENT '手机号 MD5（脱敏后）',
  register_time  string       COMMENT '注册时间',
  user_level     int          COMMENT '用户等级',
  city_id        string       COMMENT '城市id'
)
COMMENT '用户信息（手机号已脱敏）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dwd.dwd_sku_info (
  sku_id        string        COMMENT '商品id',
  sku_name      string        COMMENT '商品名',
  category_id   string        COMMENT '类目id',
  category_name string        COMMENT '类目名',
  brand_id      string        COMMENT '品牌id',
  price         decimal(18,2) COMMENT '标准售价'
)
COMMENT '商品信息'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dwd.dwd_payment (
  payment_id    string        COMMENT '支付流水id',
  order_id      string        COMMENT '订单id',
  pay_channel   string        COMMENT '支付渠道',
  pay_amount    decimal(18,2) COMMENT '支付金额',
  pay_status    int           COMMENT '支付状态',
  pay_time      string        COMMENT '支付时间'
)
COMMENT '支付流水（清洗后）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;
