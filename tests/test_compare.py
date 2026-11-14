from __future__ import annotations

import pytest

from conftest import REPORT_DIR
from koujing.compare import (
    align_findings,
    compare_dictionary_versions,
    compare_report,
    compare_reports,
    failing_categories,
    sort_findings,
    summarise,
)
from koujing.dict_load import dictionary_from_document
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
# Cross-dictionary comparison
# ---------------------------------------------------------------------------

def metric_entry(**overrides):
    entry = {
        "metric": "sample_metric",
        "owner": "sample_owner",
        "description": "synthetic fixture metric",
        "source_tables": ["fact_sample"],
        "source_fields": ["amount", "event_at", "status"],
        "aggregation": "sum",
        "filter_expr": "status = 'ok'",
        "granularity": "day",
        "version": "1.0.0",
    }
    entry.update(overrides)
    return entry


def dictionary_of(*entries):
    return dictionary_from_document({"metrics": list(entries)})


def migration_for(sql, baseline_dict, current_dict, metric="sample_metric"):
    parsed = parse_sql(sql, mode="fallback")
    baseline = compare_reports([("fixture.sql", parsed)], baseline_dict)
    current = compare_reports([("fixture.sql", parsed)], current_dict)
    migrations = align_findings(baseline, current, baseline_dict, current_dict)
    matches = [item for item in migrations if item.metric == metric]
    assert len(matches) == 1
    return matches[0]


CONSISTENT_SQL = """
SELECT DATE(event_at) AS event_date, SUM(amount) AS sample_metric
FROM fact_sample
WHERE status = 'ok'
GROUP BY DATE(event_at);
"""

DRIFT_SQL = """
SELECT DATE(event_at) AS event_date, COUNT(amount) AS sample_metric
FROM fact_sample
WHERE status = 'ok'
GROUP BY DATE(event_at);
"""


def test_same_definition_is_unchanged():
    dictionary = dictionary_of(metric_entry())
    migration = migration_for(CONSISTENT_SQL, dictionary, dictionary)
    assert migration.status == "unchanged"
    assert migration.baseline_category == migration.current_category == "consistent"


def test_consistent_to_drift_is_tightened_and_reverse_is_relaxed():
    strict = dictionary_of(metric_entry(aggregation="sum"))
    drifting_sql = DRIFT_SQL.replace("COUNT(amount)", "SUM(amount)")
    # Current dictionary expects COUNT while report uses a different aggregate.
    loose = dictionary_of(metric_entry(aggregation="avg"))

    migration = migration_for(drifting_sql, strict, loose)
    assert (migration.baseline_category, migration.current_category) == (
        "consistent", "drift"
    )
    assert migration.status == "tightened"

    reverse = migration_for(drifting_sql, loose, strict)
    assert (reverse.baseline_category, reverse.current_category) == (
        "drift", "consistent"
    )
    assert reverse.status == "relaxed"


def test_drift_to_consistent_is_relaxed_not_tightened():
    # Baseline expects count_distinct; current expects count, which matches the
    # drifting report. This is a loosening of the definition.
    migration = migration_for(
        DRIFT_SQL,
        dictionary_of(metric_entry(aggregation="count_distinct")),
        dictionary_of(metric_entry(aggregation="count")),
    )
    assert migration.baseline_category == "drift"
    assert migration.current_category == "consistent"
    assert migration.status == "relaxed"


def test_metric_absent_from_baseline_is_newly_registered():
    migration = migration_for(
        CONSISTENT_SQL, dictionary_of(metric_entry(metric="other_metric")),
        dictionary_of(metric_entry()),
    )
    assert migration.baseline_category == "unregistered"
    assert migration.current_category == "consistent"
    assert migration.status == "newly_registered"


def test_metric_removed_from_current_dictionary_is_dictionary_removed():
    migration = migration_for(
        CONSISTENT_SQL, dictionary_of(metric_entry()),
        dictionary_of(metric_entry(metric="other_metric")),
    )
    assert migration.baseline_category == "consistent"
    assert migration.current_category == "unregistered"
    assert migration.status == "dictionary_removed"


def test_dict_removal_is_distinct_from_staying_unregistered():
    # Both runs leave the alias unregistered: that is "unchanged", not removal.
    migration = migration_for(
        CONSISTENT_SQL, dictionary_of(metric_entry(metric="other_metric")),
        dictionary_of(metric_entry(metric="another_metric")),
    )
    assert migration.baseline_category == migration.current_category == "unregistered"
    assert migration.status == "unchanged"


def test_granularity_change_moves_consistent_to_drift():
    granularity_sql = """
    SELECT DATE(event_at) AS event_date, SUM(amount) AS sample_metric
    FROM fact_sample
    WHERE status = 'ok'
    GROUP BY DATE(event_at);
    """
    migration = migration_for(
        granularity_sql,
        dictionary_of(metric_entry(granularity="day")),
        dictionary_of(metric_entry(granularity="month")),
    )
    assert (migration.baseline_category, migration.current_category) == (
        "consistent", "drift"
    )
    assert migration.status == "tightened"


def test_filter_change_moves_consistent_to_drift():
    filter_sql = """
    SELECT DATE(event_at) AS event_date, SUM(amount) AS sample_metric
    FROM fact_sample
    WHERE status = 'ok'
    GROUP BY DATE(event_at);
    """
    migration = migration_for(
        filter_sql,
        dictionary_of(metric_entry(filter_expr="status = 'ok'")),
        dictionary_of(metric_entry(filter_expr="status = 'paid'")),
    )
    assert (migration.baseline_category, migration.current_category) == (
        "consistent", "drift"
    )
    assert migration.status == "tightened"


def test_repeated_metric_is_paired_per_aggregate_expression():
    two_metrics_sql = """
    SELECT DATE(event_at) AS event_date, SUM(amount) AS sample_metric,
           SUM(other_amount) AS sample_metric
    FROM fact_sample
    WHERE status = 'ok'
    GROUP BY DATE(event_at);
    """
    baseline = dictionary_of(metric_entry())
    current = dictionary_of(metric_entry())
    parsed = parse_sql(two_metrics_sql, mode="fallback")
    baseline_findings = compare_reports([("fixture.sql", parsed)], baseline)
    current_findings = compare_reports([("fixture.sql", parsed)], current)
    migrations = align_findings(
        baseline_findings, current_findings, baseline, current
    )
    assert len(migrations) == 2
    assert {item.aggregate_expression for item in migrations} == {
        "amount", "other_amount"
    }
    assert all(item.status == "unchanged" for item in migrations)


def test_repeated_metric_does_not_misalign_when_categories_differ():
    # Two occurrences share the report + metric + aggregate expression but use
    # different functions; pairing must follow the occurrence discriminator, so
    # the SUM pair stays consistent while the COUNT pair flips between runs.
    two_metrics_sql = """
    SELECT DATE(event_at) AS event_date, SUM(amount) AS sample_metric,
           COUNT(amount) AS sample_metric
    FROM fact_sample
    WHERE status = 'ok'
    GROUP BY DATE(event_at);
    """
    baseline = dictionary_of(metric_entry(aggregation="sum"))
    current = dictionary_of(metric_entry(aggregation="count"))
    parsed = parse_sql(two_metrics_sql, mode="fallback")
    baseline_findings = compare_reports([("fixture.sql", parsed)], baseline)
    current_findings = compare_reports([("fixture.sql", parsed)], current)
    migrations = align_findings(
        baseline_findings, current_findings, baseline, current
    )
    by_function = {item.aggregate_function: item for item in migrations}
    assert by_function["sum"].baseline_category == "consistent"
    assert by_function["sum"].current_category == "drift"
    assert by_function["sum"].status == "tightened"
    assert by_function["count"].baseline_category == "drift"
    assert by_function["count"].current_category == "consistent"
    assert by_function["count"].status == "relaxed"


def test_evolution_summary_aggregates_by_metric_and_counts():
    dictionary = dictionary_of(metric_entry(), metric_entry(metric="second_metric"))
    current = dictionary_of(metric_entry(aggregation="avg"))
    findings = reports_for(dictionary)
    baseline_findings = findings
    current_findings = compare_reports(
        [(f"fixtures/reports/{path.name}",
          parse_sql(path.read_text(encoding="utf-8"), mode="fallback"))
         for path in sorted(REPORT_DIR.glob("*.sql"))],
        current,
    )
    evolution = compare_dictionary_versions(
        baseline_findings, current_findings, dictionary, current
    )
    assert sum(evolution.counts().values()) == len(evolution.migrations)
    assert set(evolution.by_metric()) <= {
        "active_user", "order_gmv", "weekly_retention"
    }
    assert all(
        item.status != "unchanged" for item in evolution.changes()
    )
