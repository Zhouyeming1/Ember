-- DWD -> DIM：用户维度。CTE 先去重（同一天可能有多条快照），再补等级名。
-- 这是全公司共用的用户维度，改这里的口径会横向影响所有主题域。
INSERT OVERWRITE TABLE dim.dim_user PARTITION (dt)
SELECT
  t.user_id,
  t.user_name,
  t.phone_md5,
  t.user_level,
  CASE t.user_level
    WHEN 1 THEN '普通'
    WHEN 2 THEN '银卡'
    WHEN 3 THEN '金卡'
    WHEN 4 THEN '钻石'
    ELSE '未知'
  END AS user_level_name,
  t.city_id,
  datediff('${bizdate}', substr(t.register_time, 1, 10)) AS register_days,
  '${bizdate}' AS dt
FROM (
  SELECT
    user_id,
    user_name,
    phone_md5,
    user_level,
    city_id,
    register_time,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY register_time DESC) AS rn
  FROM dwd.dwd_user_info
  WHERE dt = ${bizdate}
) t
WHERE t.rn = 1
;
