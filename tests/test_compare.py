from __future__ import annotations

import pytest

from conftest import REPORT_DIR, valid_metric
from koujing.compare import (
    compare_report,
    compare_reports,
    failing_categories,
    sort_findings,
    summarise,
)
from koujing.dict_load import dictionary_from_document
from koujing.sql_parse import parse_sql
from koujing.sql_parse.model import UNKNOWN_DAY_ORIGIN


def reports_for(dictionary):
    items = []
    for path in sorted(REPORT_DIR.glob("*.sql")):
        parsed = parse_sql(path.read_text(encoding="utf-8"), mode="fallback")
        items.append((f"fixtures/reports/{path.name}", parsed))
    return compare_reports(items, dictionary)


def test_all_categories_are_covered(dictionary):
    findings = reports_for(dictionary)
    assert summarise(findings) == {
        "consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1
    }


@pytest.mark.parametrize("category, positive, negative", [
    ("consistent", "ok_order_gmv.sql", "drift_active_user.sql"),
    ("drift", "drift_active_user.sql", "ok_order_gmv.sql"),
    ("missing_field", "missing_field.sql", "ok_order_gmv.sql"),
    ("unregistered", "unregistered_metric.sql", "drift_active_user.sql"),
])
def test_each_category_has_positive_and_negative_cases(dictionary, category, positive, negative):
    findings = reports_for(dictionary)
    positive_categories = {
        item.category for item in findings if item.report.endswith(positive)
    }
    negative_categories = {
        item.category for item in findings if item.report.endswith(negative)
    }
    assert category in positive_categories
    assert category not in negative_categories


def test_drift_reports_exact_aggregation_difference(dictionary):
    findings = [item for item in reports_for(dictionary) if item.category == "drift"]
    assert len(findings) == 1
    difference = findings[0].differences[0]
    assert difference == {
        "field": "aggregation",
        "expected": "count_distinct",
        "actual": "count",
    }


def test_filter_drift_is_also_detected(dictionary):
    sql = """
    SELECT SUM(pay_amount) AS order_gmv
    FROM fact_order
    WHERE order_status = 'refunded'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "drift"
    assert any(item["field"] == "filter_expr" for item in finding.differences)


def test_table_or_granularity_drift_is_reported_as_drift(dictionary):
    sql = """
    SELECT SUM(pay_amount) AS order_gmv
    FROM fact_order_archive
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "drift"
    assert any(item["field"] == "source_tables" for item in finding.differences)


def test_extra_selected_dimension_is_missing_field(dictionary):
    sql = """
    SELECT SUM(pay_amount) AS order_gmv, channel
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "missing_field"
    fields = next(item for item in finding.differences if item["field"] == "source_fields")
    assert fields["undeclared_in_report"] == ["channel"]


def test_missing_field_has_priority_over_drift(dictionary):
    findings = [item for item in reports_for(dictionary)
                if item.report.endswith("missing_field.sql")]
    assert findings[0].category == "missing_field"
    fields = next(item for item in findings[0].differences
                  if item["field"] == "source_fields")
    assert fields["undeclared_in_report"] == ["coupon_amount"]
    assert fields["missing_in_report"] == []


def test_evidence_contains_required_audit_fields(dictionary):
    finding = reports_for(dictionary)[0]
    assert finding.line_start >= 2
    assert finding.line_end >= finding.line_start
    assert finding.parser_source == "fallback"
    assert finding.evidence["report"] == finding.report
    assert finding.evidence["line_range"] == [finding.line_start, finding.line_end]
    assert finding.evidence["parser_source"] == "fallback"
    assert isinstance(finding.evidence["warnings"], list)


def test_comparison_is_deep_equal_on_repeat(dictionary):
    first = reports_for(dictionary)
    second = reports_for(dictionary)
    assert first == second
    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert first == sort_findings(first)


def test_failing_categories_stable_order(dictionary):
    findings = reports_for(dictionary)
    assert failing_categories(findings, ["unregistered", "drift"]) == [
        "drift", "unregistered"
    ]
    assert failing_categories(findings, []) == []
    assert failing_categories(findings, ["missing_field"]) == ["missing_field"]


def test_unregistered_evidence_has_no_expected_metric(dictionary):
    finding = next(item for item in reports_for(dictionary)
                   if item.category == "unregistered")
    assert finding.metric == "weekly_retention"
    assert finding.evidence["expected"] is None
    assert finding.evidence["observed"]["tables"] == ["fact_session"]


# ---------------------------------------------------------------------------
# Business-day origin reconciliation (UTC timestamps + constant minute offset)
# ---------------------------------------------------------------------------

def _day_metric(**overrides):
    metric = valid_metric(
        source_tables=["fact_sample"],
        source_fields=["amount", "event_at", "status"],
        granularity="day",
    )
    metric.update(overrides)
    return metric


def _day_report(day_expression: str) -> str:
    return (
        "SELECT SUM(amount) AS sample_metric FROM fact_sample "
        f"WHERE status = 'ok' GROUP BY {day_expression};"
    )


def _finding_for(metric, sql, *, mode="fallback"):
    dictionary = dictionary_from_document({"metrics": [metric]})
    return compare_report("fixture.sql", parse_sql(sql, mode=mode), dictionary)[0]


def test_minute_and_hour_offsets_are_equivalent_240_minutes():
    """INTERVAL '4' HOUR and INTERVAL '240' MINUTE carry the same origin."""
    metric = _day_metric(business_day_start_minutes=240)
    hour_sql = _day_report("DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)")
    minute_sql = _day_report("DATE_TRUNC('day', event_at - INTERVAL '240' MINUTE)")

    hour_finding = _finding_for(metric, hour_sql)
    minute_finding = _finding_for(metric, minute_sql)

    assert hour_finding.category == "consistent"
    assert minute_finding.category == "consistent"
    assert hour_finding.evidence["observed"]["day_origin_minutes"] == 240
    assert minute_finding.evidence["observed"]["day_origin_minutes"] == 240
    assert not any(
        diff["field"] == "business_day_start_minutes"
        for diff in hour_finding.differences
    )


def test_zero_and_four_hour_origin_produce_granularity_difference_evidence():
    """Same 'day' label but different origins must not be treated as equal."""
    metric = _day_metric(business_day_start_minutes=240)
    midnight_sql = _day_report("DATE_TRUNC('day', event_at)")
    four_hour_sql = _day_report("DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)")

    midnight = _finding_for(metric, midnight_sql)
    four_hour = _finding_for(metric, four_hour_sql)

    # The 04:00 report matches a 04:00 dictionary declaration.
    assert four_hour.category == "consistent"
    assert four_hour.evidence["observed"]["granularity"] == "day"
    assert four_hour.evidence["observed"]["day_origin_minutes"] == 240

    # The midnight report is still granularity 'day' but its origin differs.
    assert midnight.category == "drift"
    difference = next(diff for diff in midnight.differences
                      if diff["field"] == "business_day_start_minutes")
    assert difference == {"field": "business_day_start_minutes",
                          "expected": 240, "actual": 0}
    # No plain granularity-label difference: both sides are genuinely 'day'.
    assert not any(diff["field"] == "granularity" for diff in midnight.differences)


def test_dynamic_offset_is_explicitly_undecidable_not_silent_day():
    """A non-constant offset must surface as unknown and never match silently."""
    metric = _day_metric(
        source_fields=["amount", "event_at", "offset_minutes", "status"],
        business_day_start_minutes=240,
    )
    sql = _day_report(
        "DATE_TRUNC('day', event_at - offset_minutes * INTERVAL '1' MINUTE)"
    )
    finding = _finding_for(metric, sql)

    assert finding.category == "drift"
    assert finding.evidence["observed"]["granularity"] == "day"
    assert finding.evidence["observed"]["day_origin_minutes"] == "unknown"
    difference = next(diff for diff in finding.differences
                      if diff["field"] == "business_day_start_minutes")
    assert difference == {"field": "business_day_start_minutes",
                          "expected": 240, "actual": "unknown"}


def test_undecidable_origin_does_not_match_a_zero_origin_dictionary():
    """Unknown must not collapse to zero, even when zero is the expected origin."""
    metric = _day_metric(business_day_start_minutes=0)
    sql = _day_report(
        "DATE_TRUNC('day', event_at AT TIME ZONE 'UTC')"
    )
    finding = _finding_for(metric, sql)
    assert finding.category == "drift"
    difference = next(diff for diff in finding.differences
                      if diff["field"] == "business_day_start_minutes")
    assert difference["expected"] == 0
    assert difference["actual"] == "unknown"


def test_legacy_dictionary_without_field_keeps_granularity_only_rule():
    """Old dictionaries keep the legacy rule and never raise origin drift."""
    legacy_metric = _day_metric()  # no business_day_start_minutes
    midnight_sql = _day_report("DATE_TRUNC('day', event_at)")
    four_hour_sql = _day_report("DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)")

    midnight = _finding_for(legacy_metric, midnight_sql)
    four_hour = _finding_for(legacy_metric, four_hour_sql)

    # Both are 'day'; without a declared origin there is no origin difference.
    assert midnight.category == "consistent"
    assert four_hour.category == "consistent"
    assert not any(
        diff["field"] == "business_day_start_minutes"
        for diff in midnight.differences + four_hour.differences
    )
    # The observed origin is still recorded for the report and ledger.
    assert midnight.evidence["observed"]["day_origin_minutes"] == 0
    assert four_hour.evidence["observed"]["day_origin_minutes"] == 240
    # The legacy expected metric explicitly carries an unset origin.
    assert midnight.evidence["expected"]["business_day_start_minutes"] is None


def test_observed_and_expected_day_origin_are_both_retained_in_evidence():
    """Reports and ledgers must preserve expected and actual day origins."""
    metric = _day_metric(business_day_start_minutes=240)
    sql = _day_report("DATE_TRUNC('day', event_at - INTERVAL '1' HOUR)")
    finding = _finding_for(metric, sql)

    assert finding.evidence["expected"]["business_day_start_minutes"] == 240
    assert finding.evidence["observed"]["day_origin_minutes"] == 60
    difference = next(diff for diff in finding.differences
                      if diff["field"] == "business_day_start_minutes")
    assert difference["expected"] == 240
    assert difference["actual"] == 60
    # The serialised form (what the ledger persists) keeps both values.
    serialised = finding.to_dict()
    assert serialised["evidence"]["expected"]["business_day_start_minutes"] == 240
    assert serialised["evidence"]["observed"]["day_origin_minutes"] == 60


@pytest.mark.parametrize("expression, expected_origin", [
    ("DATE_TRUNC('day', event_at - INTERVAL '4' HOUR)", 240),
    ("DATE_TRUNC('day', event_at - INTERVAL '240' MINUTE)", 240),
    ("DATE_TRUNC('day', event_at)", 0),
    ("DATE(event_at)", 0),
    ("CAST(event_at AS DATE)", 0),
    # A positive SQL shift wraps to the previous-day boundary.
    ("DATE_TRUNC('day', event_at + INTERVAL '4' HOUR)", 1200),
    ("DATE_TRUNC('day', event_at - offset_minutes * INTERVAL '1' MINUTE)",
     UNKNOWN_DAY_ORIGIN),
    ("DATE_TRUNC('month', event_at)", None),
])
def test_primary_and_fallback_report_identical_day_origin(expression, expected_origin):
    """Both parsers must expose the same business-day origin structure."""
    pytest.importorskip("sqlglot")
    sql = _day_report(expression)
    fallback = parse_sql(sql, mode="fallback")
    primary = parse_sql(sql, mode="primary")
    assert fallback.comparable() == primary.comparable()
    assert fallback.day_origin_minutes == expected_origin
    assert primary.day_origin_minutes == expected_origin
