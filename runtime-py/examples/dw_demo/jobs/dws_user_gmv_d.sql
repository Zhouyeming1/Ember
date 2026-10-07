-- DWD -> DWS：用户日粒度 GMV
-- 注意 gmv 是明细 amount 求和。DWS 的口径会被多个 ADS 复用，
-- 改 gmv 的含义（比如改成扣退款）会同时影响用户报表和 GMV 大盘。
INSERT OVERWRITE TABLE dws.dws_user_gmv_d PARTITION (dt)
SELECT
  d.user_id,
  SUM(d.amount) AS gmv,
  COUNT(DISTINCT d.order_id) AS order_cnt,
  COUNT(DISTINCT d.sku_id) AS sku_cnt,
  d.dt
FROM dwd.dwd_order_detail d
WHERE d.dt = ${bizdate}
  AND d.order_status IN (2, 4)
GROUP BY d.user_id, d.dt
;
