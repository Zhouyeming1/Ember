-- DWD -> DWS：订单日汇总，带上支付信息。
-- 用 CTE 把两张明细表各自收口，避免在 join 里堆大表达式。
INSERT OVERWRITE TABLE dws.dws_order_summary_d PARTITION (dt)
WITH order_agg AS (
  SELECT
    order_id,
    user_id,
    SUM(amount) AS order_amount,
    MAX(order_status) AS order_status
  FROM dwd.dwd_order_detail
  WHERE dt = ${bizdate}
  GROUP BY order_id, user_id
),
pay_agg AS (
  SELECT
    order_id,
    SUM(pay_amount) AS pay_amount,
    MAX(pay_channel) AS pay_channel
  FROM dwd.dwd_payment
  WHERE dt = ${bizdate}
  GROUP BY order_id
)
SELECT
  o.order_id,
  o.user_id,
  o.order_amount,
  COALESCE(p.pay_amount, 0) AS pay_amount,
  COALESCE(p.pay_channel, 'unknown') AS pay_channel,
  o.order_status,
  '${bizdate}' AS dt
FROM order_agg o
LEFT JOIN pay_agg p
  ON o.order_id = p.order_id
;
