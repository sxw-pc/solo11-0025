-- Consistent average ratio: the ratio is formed per row, then averaged.
SELECT
  DATE(order_paid_at) AS order_date,
  AVG(pay_amount / NULLIF(quantity, 0)) AS avg_basket_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
