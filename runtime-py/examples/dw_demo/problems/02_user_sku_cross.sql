-- 【埋的问题：笛卡尔积 + LIKE 前置通配符】
-- 做"用户×商品"交叉试验表时图省事用了逗号连接，漏了 u.city_id 和 s.brand_id 的关联，
-- 行数直接是 用户数 × 商品数。分类过滤又写成了前置通配符。
-- 预期：sql_lint 报 cartesian-join（警告）+ like-leading-wildcard（提示）。

INSERT OVERWRITE TABLE ads.ads_user_sku_cross PARTITION (dt)
SELECT
  u.user_id,
  s.sku_id,
  '${bizdate}' AS dt
FROM dim.dim_user u, dim.dim_sku s
WHERE u.dt = '${bizdate}'
  AND s.dt = '${bizdate}'
  AND s.sku_name LIKE '%手机%'
;
