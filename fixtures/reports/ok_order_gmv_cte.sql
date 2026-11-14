-- CTE lineage: order_gmv must trace back through `paid` to fact_order.pay_amount.
WITH paid AS (
  SELECT
    pay_amount,
    order_paid_at,
    order_status
  FROM fact_order
)
SELECT
  DATE(p.order_paid_at) AS order_date,
  SUM(p.pay_amount) AS order_gmv
FROM paid p
WHERE p.order_status = 'paid'
GROUP BY DATE(p.order_paid_at);
