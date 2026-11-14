-- Intentional drift: numerator and denominator are swapped.
SELECT
  DATE(order_paid_at) AS order_date,
  COUNT(DISTINCT order_id) / NULLIF(SUM(pay_amount), 0) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
