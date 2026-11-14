-- Equivalent spelling: duplicate AND conjunct and redundant outer parentheses.
SELECT
  DATE(login_at) AS login_date,
  COUNT(DISTINCT user_id) AS active_user
FROM fact_login
WHERE (event_type = 'login' AND status = 'ok' AND event_type = 'login')
GROUP BY DATE(login_at);
