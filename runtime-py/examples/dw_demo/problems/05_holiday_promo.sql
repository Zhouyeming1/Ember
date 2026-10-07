-- 【埋的问题：分区过滤写死日期】
-- 事后复盘双十一活动时写死了一天。脚本进了调度以后，每天跑的其实都是 2024-01-01，
-- 而且不会报错——数据一直是"对的"，只是永远不对当天。
-- 预期：sql_lint 报 hardcoded-date。

INSERT OVERWRITE TABLE ads.ads_holiday_promo PARTITION (dt)
SELECT
  d.user_id,
  SUM(d.amount) AS promo_gmv,
  '20240101' AS dt
FROM dwd.dwd_order_detail d
WHERE d.dt = '2024-01-01'
GROUP BY d.user_id
;
