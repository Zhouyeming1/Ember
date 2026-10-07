-- DWD -> DIM：商品维度
INSERT OVERWRITE TABLE dim.dim_sku PARTITION (dt)
SELECT
  sku_id,
  sku_name,
  category_id,
  category_name,
  brand_id,
  price,
  dt
FROM dwd.dwd_sku_info
WHERE dt = ${bizdate}
;
