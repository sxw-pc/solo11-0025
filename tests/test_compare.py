from __future__ import annotations

import pytest

from conftest import REPORT_DIR
from koujing.compare import (
    compare_report,
    compare_reports,
    failing_categories,
    sort_findings,
    summarise,
)
from koujing.sql_parse import parse_sql


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
    assert difference["field"] == "aggregation"
    assert difference["expected"] == "count_distinct"
    assert difference["actual"] == "count"
    assert difference["kind"] == "real_deviation"
    assert difference["label"] == "真实偏差"


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
