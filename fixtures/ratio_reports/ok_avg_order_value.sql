-- Consistent: SUM(pay_amount) / NULLIF(COUNT(DISTINCT order_id), 0).
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) / NULLIF(COUNT(DISTINCT order_id), 0) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
