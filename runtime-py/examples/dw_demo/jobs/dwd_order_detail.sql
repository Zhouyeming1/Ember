-- ODS -> DWD：订单明细宽表
-- 明细 join 主表补订单状态。两侧都要带 dt 过滤，否则会扫全表分区。
-- 调度：DolphinScheduler 每天 02:10，bizdate = 昨天。

INSERT OVERWRITE TABLE dwd.dwd_order_detail PARTITION (dt)
SELECT
  d.detail_id,
  d.order_id,
  d.user_id,
  d.sku_id,
  d.quantity,
  d.amount,
  d.discount,
  o.order_status,
  d.create_time,
  d.dt
FROM ods.ods_order_detail d
JOIN ods.ods_order o
  ON d.order_id = o.order_id
 AND d.dt = o.dt
WHERE d.dt = ${bizdate}
  AND o.dt = ${bizdate}
;
