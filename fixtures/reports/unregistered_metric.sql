-- Intentional unregistered metric alias.
SELECT
  DATE(session_started_at) AS session_day,
  COUNT(DISTINCT session_id) AS weekly_retention
FROM fact_session
WHERE platform = 'web'
GROUP BY DATE(session_started_at);
