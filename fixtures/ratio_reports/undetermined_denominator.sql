-- Denominator uses a function outside the supported subset: undetermined.
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) / NULLIF(percentile_approx(quantity, 0.5), 0) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
