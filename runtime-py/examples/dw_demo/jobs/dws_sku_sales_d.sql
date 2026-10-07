-- DWD -> DWS：商品日粒度销量，join 商品维度补类目
INSERT OVERWRITE TABLE dws.dws_sku_sales_d PARTITION (dt)
SELECT
  d.sku_id,
  s.category_id,
  SUM(d.amount) AS sales_amount,
  SUM(d.quantity) AS sales_qty,
  COUNT(DISTINCT d.user_id) AS buyer_cnt,
  d.dt
FROM dwd.dwd_order_detail d
JOIN dim.dim_sku s
  ON d.sku_id = s.sku_id
 AND d.dt = s.dt
WHERE d.dt = ${bizdate}
  AND s.dt = ${bizdate}
GROUP BY d.sku_id, s.category_id, d.dt
;
