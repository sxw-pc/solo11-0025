-- Consistent: paid order GMV.
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) AS order_gmv
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
