-- DWS + DIM -> ADS：用户经营报表（BI 直连）
-- gmv 直接来自 dws.dws_user_gmv_d，所以 DWS 改口径这里会跟着变——
-- 这正是"改一个字段、下游炸一片"的典型位置。
INSERT OVERWRITE TABLE ads.ads_user_report PARTITION (dt)
SELECT
  g.user_id,
  u.user_name,
  u.user_level_name,
  g.gmv,
  g.order_cnt,
  g.dt
FROM dws.dws_user_gmv_d g
JOIN dim.dim_user u
  ON g.user_id = u.user_id
 AND g.dt = u.dt
WHERE g.dt = ${bizdate}
  AND u.dt = ${bizdate}
;
