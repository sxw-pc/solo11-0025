-- Real drift: OR chain order is deliberately reversed, which changes semantics
-- relative to the registered filter and must not be treated as equivalent.
SELECT
  DATE(login_at) AS login_date,
  COUNT(DISTINCT user_id) AS active_user
FROM fact_login
WHERE status = 'ok' OR event_type = 'login'
GROUP BY DATE(login_at);
