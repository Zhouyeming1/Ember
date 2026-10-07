-- ODS 层：贴源表。结构与上游业务库一致，按业务日期 + 小时分区。
-- 落库方式是每日从 MySQL 抽数（DataX / Flink CDC），这里只放表结构。

CREATE TABLE IF NOT EXISTS ods.ods_order (
  order_id      string        COMMENT '订单id',
  user_id       string        COMMENT '用户id',
  order_status  int           COMMENT '1待支付 2已支付 3已取消 4已完成',
  total_amount  decimal(18,2) COMMENT '订单总金额',
  pay_amount    decimal(18,2) COMMENT '实付金额',
  create_time   string        COMMENT '下单时间 yyyy-MM-dd HH:mm:ss',
  update_time   string        COMMENT '更新时间 yyyy-MM-dd HH:mm:ss'
)
COMMENT '订单主表（贴源）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd', hour string COMMENT '小时 HH')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ods.ods_order_detail (
  detail_id     string        COMMENT '明细id',
  order_id      string        COMMENT '订单id',
  user_id       string        COMMENT '用户id',
  sku_id        string        COMMENT '商品id',
  quantity      int           COMMENT '数量',
  amount        decimal(18,2) COMMENT '明细金额',
  discount      decimal(18,2) COMMENT '优惠金额',
  create_time   string        COMMENT '创建时间 yyyy-MM-dd HH:mm:ss'
)
COMMENT '订单明细（贴源）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd', hour string COMMENT '小时 HH')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ods.ods_user (
  user_id       string        COMMENT '用户id',
  user_name     string        COMMENT '用户名',
  phone         string        COMMENT '手机号（明文，DWD 层脱敏）',
  register_time string        COMMENT '注册时间 yyyy-MM-dd HH:mm:ss',
  user_level    int           COMMENT '1普通 2银卡 3金卡 4钻石',
  city_id       string        COMMENT '城市id'
)
COMMENT '用户表（贴源）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ods.ods_sku (
  sku_id        string        COMMENT '商品id',
  sku_name      string        COMMENT '商品名',
  category_id   string        COMMENT '类目id',
  category_name string        COMMENT '类目名',
  brand_id      string        COMMENT '品牌id',
  price         decimal(18,2) COMMENT '标准售价'
)
COMMENT '商品表（贴源）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS ods.ods_payment (
  payment_id    string        COMMENT '支付流水id',
  order_id      string        COMMENT '订单id',
  pay_channel   string        COMMENT 'alipay / wechat / card',
  pay_amount    decimal(18,2) COMMENT '支付金额',
  pay_status    int           COMMENT '1成功 2失败 3退款',
  pay_time      string        COMMENT '支付时间 yyyy-MM-dd HH:mm:ss'
)
COMMENT '支付流水（贴源）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;
