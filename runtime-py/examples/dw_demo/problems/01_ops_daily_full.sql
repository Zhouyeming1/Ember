-- 【埋的问题：缺分区过滤】
-- 运营要一份"每个用户每天"的全量报表。写脚本的人以为 GROUP BY dt 就等于按天取数，
-- 结果整张 dws.dws_user_gmv_d 的所有历史分区都会被扫一遍。
-- 预期：sql_lint 报 missing-partition-filter。

INSERT OVERWRITE TABLE ads.ads_ops_daily_full PARTITION (dt)
SELECT
  g.user_id,
  SUM(g.gmv) AS gmv,
  g.dt
FROM dws.dws_user_gmv_d g
GROUP BY g.user_id, g.dt
;
