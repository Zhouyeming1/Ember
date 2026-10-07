-- 【埋的问题：INSERT OVERWRITE 分区表却没写 PARTITION】
-- 这是最危险的一类：看上去是一次日更，实际会把 dws.dws_user_gmv_d 的**所有**
-- 历史分区覆盖掉，只剩 bizdate 那一天。第二天全公司的看板都会变。
-- 正确写法是 INSERT OVERWRITE TABLE ... PARTITION (dt)，让 dt 跟着 SELECT 走。
-- 预期：sql_lint 报 overwrite-without-partition（错误级）。

INSERT OVERWRITE TABLE dws.dws_user_gmv_d
SELECT
  d.user_id,
  SUM(d.amount) AS gmv,
  COUNT(DISTINCT d.order_id) AS order_cnt,
  COUNT(DISTINCT d.sku_id) AS sku_cnt,
  d.dt
FROM dwd.dwd_order_detail d
WHERE d.dt = ${bizdate}
GROUP BY d.user_id, d.dt
;
