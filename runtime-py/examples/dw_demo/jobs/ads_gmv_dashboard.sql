-- DWS -> ADS：GMV 大盘。三个来源 union 成全量口径 + 类目口径。
-- 这张表 BI 看板直连，字段口径变更需要同步业务方。
INSERT OVERWRITE TABLE ads.ads_gmv_dashboard PARTITION (dt)
SELECT
  'all' AS stat_dim,
  'ALL' AS stat_key,
  SUM(g.gmv) AS total_gmv,
  SUM(s.pay_amount) AS pay_gmv,
  SUM(g.order_cnt) AS order_cnt,
  COUNT(DISTINCT g.user_id) AS buyer_cnt,
  g.dt
FROM dws.dws_user_gmv_d g
LEFT JOIN dws.dws_order_summary_d s
  ON g.dt = s.dt
 AND g.user_id = s.user_id
WHERE g.dt = ${bizdate}
  AND s.dt = ${bizdate}
GROUP BY g.dt

UNION ALL

SELECT
  'category' AS stat_dim,
  k.category_id AS stat_key,
  SUM(k.sales_amount) AS total_gmv,
  CAST(0 AS decimal(18,2)) AS pay_gmv,
  COUNT(DISTINCT k.sku_id) AS order_cnt,
  SUM(k.buyer_cnt) AS buyer_cnt,
  k.dt
FROM dws.dws_sku_sales_d k
WHERE k.dt = ${bizdate}
GROUP BY k.category_id, k.dt
;
