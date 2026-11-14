-- Equivalent spelling: AND conjuncts in a different order than the dictionary.
SELECT
  DATE(login_at) AS login_date,
  COUNT(DISTINCT user_id) AS active_user
FROM fact_login
WHERE status = 'ok' AND event_type = 'login'
GROUP BY DATE(login_at);
