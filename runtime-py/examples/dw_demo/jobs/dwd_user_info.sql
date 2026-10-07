-- ODS -> DWD：用户信息，手机号做 MD5 脱敏（合规要求，DWD 以上不再有明文）
INSERT OVERWRITE TABLE dwd.dwd_user_info PARTITION (dt)
SELECT
  user_id,
  user_name,
  md5(concat(phone, 'ember_salt')) AS phone_md5,
  register_time,
  user_level,
  city_id,
  dt
FROM ods.ods_user
WHERE dt = ${bizdate}
;
