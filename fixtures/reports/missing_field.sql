-- Intentional missing field: coupon_amount is not declared by the metric.
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) AS order_gmv
FROM fact_order
WHERE order_status = 'paid'
  AND coupon_amount > 0
GROUP BY DATE(order_paid_at);
