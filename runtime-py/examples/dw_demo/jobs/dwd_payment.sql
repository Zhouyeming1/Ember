-- ODS -> DWD：支付流水，只留成功的支付（退款/失败在 DWS 单独处理）
INSERT OVERWRITE TABLE dwd.dwd_payment PARTITION (dt)
SELECT
  payment_id,
  order_id,
  pay_channel,
  pay_amount,
  pay_status,
  pay_time,
  dt
FROM ods.ods_payment
WHERE dt = ${bizdate}
  AND pay_status = 1
;
