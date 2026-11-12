-- Intentional drift: user_id is counted without DISTINCT.
SELECT
  DATE(login_at) AS login_date,
  COUNT(user_id) AS active_user
FROM fact_login
WHERE event_type = 'login'
  AND status = 'ok'
GROUP BY DATE(login_at);
