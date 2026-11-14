-- Intentional drift: no zero-denominator guard.
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) / COUNT(DISTINCT order_id) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
