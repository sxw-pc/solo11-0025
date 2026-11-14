-- Intentional drift: an average ratio is used where a population ratio is declared.
SELECT
  DATE(order_paid_at) AS order_date,
  AVG(pay_amount) / NULLIF(AVG(quantity), 0) AS avg_basket_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
