from __future__ import annotations

import pytest

from conftest import read_report
from koujing.sql_parse import PrimaryParserError, parse_primary, parse_sql

sqlglot = pytest.importorskip("sqlglot")


@pytest.mark.parametrize("name", [
    "ok_order_gmv.sql",
    "ok_active_user.sql",
    "drift_active_user.sql",
    "missing_field.sql",
    "unregistered_metric.sql",
])
def test_primary_and_fallback_agree_on_structure(name):
    sql = read_report(name)
    primary = parse_primary(sql)
    fallback = parse_sql(sql, mode="fallback")
    assert primary.source == "primary"
    assert primary.comparable() == fallback.comparable()


def test_primary_count_distinct_is_normalised():
    parsed = parse_primary(read_report("ok_active_user.sql"))
    assert parsed.aggregates[0].function == "count_distinct"
    assert parsed.aggregates[0].expression == "user_id"
    assert parsed.filter_expr == "event_type = 'login' and status = 'ok'"
    assert parsed.granularity == "day"


def test_strict_primary_mode_surfaces_parse_errors():
    with pytest.raises(PrimaryParserError):
        parse_primary(read_report("weird_syntax.sql"))
    with pytest.raises(PrimaryParserError):
        parse_sql(read_report("weird_syntax.sql"), mode="primary")


def test_auto_mode_reports_fallback_origin_on_parse_failure():
    parsed = parse_sql(read_report("weird_syntax.sql"), mode="auto")
    assert parsed.source == "fallback"
    assert any("primary parser failed" in warning for warning in parsed.warnings)
    assert "\x1b" not in " ".join(parsed.warnings)


def test_primary_parser_handles_quoted_aliases():
    parsed = parse_primary(
        "SELECT SUM(amount) AS sample_metric, MAX(event_at) AS latest_event "
        "FROM fact_sample WHERE status = 'ok' GROUP BY DATE(event_at);"
    )
    assert parsed.tables == ("fact_sample",)
    assert [item.alias for item in parsed.aggregates] == ["sample_metric", "latest_event"]
    assert parsed.granularity == "day"


@pytest.mark.parametrize("expression, origin", [
    ("DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)", 240),
    ("DATE_TRUNC('day', event_at - INTERVAL '240' MINUTE)", 240),
    ("DATE_TRUNC('day', event_at)", 0),
    ("DATE_TRUNC('day', event_at - offset_minutes * INTERVAL '1' MINUTE)", -1),
])
def test_primary_and_fallback_agree_on_business_day_origin(expression, origin):
    sql = (
        "SELECT SUM(amount) AS sample_metric FROM fact_sample "
        f"WHERE status = 'ok' GROUP BY {expression};"
    )
    primary = parse_primary(sql)
    fallback = parse_sql(sql, mode="fallback")
    assert primary.comparable() == fallback.comparable()
    assert primary.granularity == "day"
    assert primary.day_origin_minutes == origin
    assert fallback.day_origin_minutes == origin
