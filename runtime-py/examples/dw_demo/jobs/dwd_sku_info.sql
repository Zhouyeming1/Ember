-- ODS -> DWD：商品信息，原样清洗（这里主要演示血缘，逻辑很薄）
INSERT OVERWRITE TABLE dwd.dwd_sku_info PARTITION (dt)
SELECT
  sku_id,
  sku_name,
  category_id,
  category_name,
  brand_id,
  price,
  dt
FROM ods.ods_sku
WHERE dt = ${bizdate}
;
