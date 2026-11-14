-- Derived-table lineage: the outer metric `o.gmv` (renamed from
-- fact_order.pay_amount inside the subquery) must trace back through the
-- derived alias `o` to its physical source. `o` is never a source table.
SELECT
  DATE(o.order_paid_at) AS order_date,
  SUM(o.gmv) AS order_gmv
FROM (
  SELECT
    pay_amount AS gmv,
    order_paid_at,
    order_status
  FROM fact_order
) o
WHERE o.order_status = 'paid'
GROUP BY DATE(o.order_paid_at);
