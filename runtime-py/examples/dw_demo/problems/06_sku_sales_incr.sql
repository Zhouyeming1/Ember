-- 【埋的问题：多表 join 用 SELECT * + 自读自写】
-- 想做增量补数，但目标是它自己（自读自写），而且用 SELECT * 直接喂回原表。
-- 两个问题叠在一起：Hive 下自读自写的行为依赖执行顺序，可能读到写了一半的数据；
-- SELECT * 又会把 join 两边的同名列一起塞进结果，列数对不上目标表。
-- 预期：sql_lint 报 select-star（提示）+ self-read-write（警告）。

INSERT OVERWRITE TABLE dws.dws_sku_sales_d PARTITION (dt)
SELECT *
FROM dws.dws_sku_sales_d t
JOIN dwd.dwd_order_detail d
  ON t.sku_id = d.sku_id
 AND t.dt = d.dt
WHERE t.dt = ${bizdate}
  AND d.dt = ${bizdate}
;
