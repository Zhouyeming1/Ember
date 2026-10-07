-- 【埋的问题：分区列被函数包裹】
-- 做用户月累计时想"取 dt 前六位等于 202401 的"，于是把分区列包进 substr。
-- 后果是分区裁剪完全失效——Hive 不能从 substr(dt,1,6)='202401' 推出要读哪些分区，
-- 于是扫全表。改成 dt BETWEEN '20240101' AND '20240131' 就能裁剪。
-- 预期：sql_lint 报 partition-column-wrapped。

INSERT OVERWRITE TABLE ads.ads_monthly_summary PARTITION (dt)
SELECT
  g.user_id,
  SUM(g.gmv) AS month_gmv,
  '${bizdate}' AS dt
FROM dws.dws_user_gmv_d g
WHERE substr(g.dt, 1, 6) = '202401'
GROUP BY g.user_id
;
