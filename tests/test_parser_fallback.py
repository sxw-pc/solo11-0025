from __future__ import annotations

from conftest import read_report
from koujing.sql_parse import parse_fallback, parse_sql


def test_parses_order_gmv_subset():
    parsed = parse_fallback(read_report("ok_order_gmv.sql"))
    assert parsed.source == "fallback"
    assert parsed.tables == ("fact_order",)
    assert parsed.select_fields == ("order_paid_at", "order_status", "pay_amount")
    assert parsed.filter_expr == "order_status = 'paid'"
    assert parsed.group_by == ("date(order_paid_at)",)
    assert parsed.granularity == "day"
    assert parsed.line_start == 2
    assert parsed.aggregates[0].function == "sum"
    assert parsed.aggregates[0].alias == "order_gmv"
    assert parsed.aggregates[0].fields == ("pay_amount",)


def test_implicit_aggregate_alias_is_supported():
    parsed = parse_fallback(
        "SELECT SUM(amount) sample_metric FROM fact_sample "
        "WHERE status = 'ok' GROUP BY DATE(event_at);"
    )
    assert parsed.aggregates[0].alias == "sample_metric"
    assert parsed.select_fields == ("amount", "event_at", "status")


def test_count_distinct_and_joins_are_tokenised():
    sql = """
    SELECT DATE(l.login_at) AS d, COUNT(DISTINCT l.user_id) AS active_user
    FROM fact_login l JOIN dim_account a ON a.user_id = l.user_id
    WHERE l.event_type = 'login'
    GROUP BY DATE(l.login_at);
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("dim_account", "fact_login")
    assert parsed.aggregates[0].function == "count_distinct"
    assert parsed.aggregates[0].expression == "user_id"
    assert "user_id" in parsed.select_fields
    assert parsed.granularity == "day"


def test_weird_syntax_returns_partial_result_with_warnings():
    parsed = parse_fallback(read_report("weird_syntax.sql"))
    assert parsed.source == "fallback"
    assert parsed.aggregates[0].alias == "active_user"
    assert parsed.tables == ("fact_login",)
    assert parsed.granularity == "day"
    assert any("EMIT" in warning for warning in parsed.warnings)
    assert any("WINDOW" in warning for warning in parsed.warnings)


def test_missing_select_does_not_crash():
    parsed = parse_fallback("UPDATE fact_order SET status = 'paid';")
    assert parsed.tables == ()
    assert parsed.aggregates == ()
    assert any("no SELECT" in warning for warning in parsed.warnings)


def test_comments_and_strings_do_not_confuse_clause_scanning():
    sql = """
    -- SELECT bogus FROM nowhere
    SELECT SUM(amount) AS sample_metric /* FROM fake */
    FROM fact_sample
    WHERE note = 'FROM hidden -- still a string'
    GROUP BY DATE(event_at);
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("fact_sample",)
    assert parsed.filter_expr == "note = 'FROM hidden -- still a string'"
    assert parsed.line_start == 3
    assert parsed.line_end == 6


def test_fallback_is_deterministic():
    sql = read_report("ok_active_user.sql")
    assert parse_fallback(sql) == parse_fallback(sql)


def test_auto_mode_falls_back_when_primary_is_unavailable(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "sqlglot", None)
    parsed = parse_sql(read_report("ok_active_user.sql"), mode="auto")
    assert parsed.source == "fallback"
    assert any("primary parser unavailable" in warning for warning in parsed.warnings)


def test_granularity_variants():
    base = "SELECT SUM(amount) AS sample_metric FROM fact_sample WHERE status = 'ok' {}"
    month = parse_fallback(base.format("GROUP BY DATE_TRUNC('month', event_at)"))
    plain = parse_fallback(base.format("GROUP BY event_at"))
    none = parse_fallback(base.format(""))
    assert month.granularity == "month"
    assert plain.granularity == "row"
    assert none.granularity == "none"


def test_business_day_origin_extraction():
    base = ("SELECT SUM(amount) AS sample_metric FROM fact_sample "
            "WHERE status = 'ok' GROUP BY {};")
    midnight = parse_fallback(base.format("DATE_TRUNC('day', event_at)"))
    four_hour = parse_fallback(base.format(
        "DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)"))
    minute_form = parse_fallback(base.format(
        "DATE_TRUNC('day', event_at - INTERVAL '240' MINUTE)"))
    dynamic = parse_fallback(base.format(
        "DATE_TRUNC('day', event_at - offset_minutes * INTERVAL '1' MINUTE)"))
    month = parse_fallback(base.format("DATE_TRUNC('month', event_at)"))

    assert midnight.granularity == "day" and midnight.day_origin_minutes == 0
    assert four_hour.granularity == "day" and four_hour.day_origin_minutes == 240
    assert minute_form.day_origin_minutes == 240
    assert dynamic.day_origin_minutes == -1
    assert month.day_origin_minutes is None
    # Interval unit words must not be mistaken for column references.
    assert "hour" not in four_hour.select_fields
    assert "minute" not in minute_form.select_fields
