-- Drift hidden behind a derived table: the metric reads the archive table
-- through subquery alias `o`. The physical source table comparison must
-- resolve through `o` to fact_order_archive instead of fact_order.
SELECT
  DATE(o.order_paid_at) AS order_date,
  SUM(o.pay_amount) AS order_gmv
FROM (
  SELECT
    pay_amount,
    order_paid_at,
    order_status
  FROM fact_order_archive
) o
WHERE o.order_status = 'paid'
GROUP BY DATE(o.order_paid_at);
