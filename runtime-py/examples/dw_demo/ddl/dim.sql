-- DIM 层：一致性维度。全公司共用一份，避免各层各写一套口径。

CREATE TABLE IF NOT EXISTS dim.dim_user (
  user_id        string       COMMENT '用户id',
  user_name      string       COMMENT '用户名',
  phone_md5      string       COMMENT '手机号 MD5',
  user_level     int          COMMENT '用户等级',
  user_level_name string      COMMENT '等级名：普通/银卡/金卡/钻石',
  city_id        string       COMMENT '城市id',
  register_days  int          COMMENT '注册距今天数'
)
COMMENT '用户维度（拉链前的日快照）'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dim.dim_sku (
  sku_id        string        COMMENT '商品id',
  sku_name      string        COMMENT '商品名',
  category_id   string        COMMENT '类目id',
  category_name string        COMMENT '类目名',
  brand_id      string        COMMENT '品牌id',
  price         decimal(18,2) COMMENT '标准售价'
)
COMMENT '商品维度'
PARTITIONED BY (dt string COMMENT '业务日期 yyyyMMdd')
STORED AS ORC;
