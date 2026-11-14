from __future__ import annotations

import pytest

from conftest import RATIO_REPORT_DIR, valid_metric
from koujing.compare import (
    compare_report,
    compare_reports,
    failing_categories,
    summarise,
    summarise_extra,
)
from koujing.dict_load import dictionary_from_document
from koujing.sql_parse import parse_sql

POPULATION_SQL = """
SELECT DATE(order_paid_at) AS order_date,
       SUM(pay_amount) / NULLIF(COUNT(DISTINCT order_id), 0) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
"""

SWAPPED_SQL = """
SELECT DATE(order_paid_at) AS order_date,
       COUNT(DISTINCT order_id) / NULLIF(SUM(pay_amount), 0) AS avg_order_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
"""

AVERAGE_SQL = """
SELECT DATE(order_paid_at) AS order_date,
       AVG(pay_amount / NULLIF(quantity, 0)) AS avg_basket_value
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
"""


def ratio_metric(**overrides):
    metric = valid_metric(
        metric="avg_order_value",
        source_tables=["fact_order"],
        source_fields=["pay_amount", "order_id", "order_paid_at", "order_status"],
        aggregation="ratio",
        filter_expr="order_status = 'paid'",
        granularity="day",
        ratio={
            "kind": "population_ratio",
            "numerator": {"aggregation": "sum", "field": "pay_amount"},
            "denominator": {"aggregation": "count_distinct", "field": "order_id"},
            "zero_division": "nullif",
        },
    )
    metric.update(overrides)
    return metric


def one_metric_dictionary(**overrides):
    return dictionary_from_document({"metrics": [ratio_metric(**overrides)]})


def ratio_findings(ratio_dictionary, mode="fallback"):
    reports = [
        (f"fixtures/ratio_reports/{path.name}",
         parse_sql(path.read_text(encoding="utf-8"), mode=mode))
        for path in sorted(RATIO_REPORT_DIR.glob("*.sql"))
    ]
    return compare_reports(reports, ratio_dictionary)


def test_ratio_fixture_batch_summary(ratio_dictionary):
    findings = ratio_findings(ratio_dictionary)
    assert summarise(findings) == {
        "consistent": 2, "drift": 3, "missing_field": 0, "unregistered": 0,
    }
    assert summarise_extra(findings) == {"undetermined": 1}


def test_ratio_parsers_agree_on_composite_structure(ratio_dictionary):
    primary = ratio_findings(ratio_dictionary, mode="primary")
    fallback = ratio_findings(ratio_dictionary, mode="fallback")
    assert [(item.report, item.category, item.metric) for item in primary] == [
        (item.report, item.category, item.metric) for item in fallback
    ]
    for left, right in zip(primary, fallback):
        assert left.differences == right.differences


def test_population_ratio_is_consistent(ratio_dictionary):
    finding = compare_report(
        "ratio.sql", parse_sql(POPULATION_SQL, mode="fallback"), ratio_dictionary
    )[0]
    assert finding.category == "consistent"
    assert finding.differences == ()
    observed = finding.evidence["observed"]["ratio"]
    assert observed["kind"] == "population_ratio"
    assert observed["zero_division"] == "nullif"
    assert observed["numerator"]["expression"] == "pay_amount"
    assert observed["denominator"]["aggregation"] == "count_distinct"


def test_avg_of_sum_is_not_treated_as_sum_ratio(ratio_dictionary):
    # AVG(x) / AVG(y) is a population ratio, not AVG(x / y).
    sql = POPULATION_SQL.replace("SUM(pay_amount)", "AVG(pay_amount)").replace(
        "COUNT(DISTINCT order_id)", "AVG(order_id)")
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             ratio_dictionary)[0]
    assert finding.evidence["observed"]["ratio"]["kind"] == "population_ratio"
    assert finding.category == "drift"


def test_swapped_operands_are_a_drift_with_specific_fields(ratio_dictionary):
    finding = compare_report(
        "ratio.sql", parse_sql(SWAPPED_SQL, mode="fallback"), ratio_dictionary
    )[0]
    assert finding.category == "drift"
    fields = {item["field"] for item in finding.differences}
    assert "ratio.operand_order" in fields
    assert fields & {"ratio.numerator.expression", "ratio.denominator.expression"}
    order = next(item for item in finding.differences
                 if item["field"] == "ratio.operand_order")
    assert order["expected"] == ["numerator", "denominator"]
    assert order["actual"] == ["denominator", "numerator"]


def test_average_ratio_vs_population_ratio_is_reported_on_kind(ratio_dictionary):
    sql = POPULATION_SQL.replace(
        "SUM(pay_amount) / NULLIF(COUNT(DISTINCT order_id), 0)",
        "AVG(pay_amount / NULLIF(order_id, 0))",
    )
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             ratio_dictionary)[0]
    assert finding.category == "drift"
    kind = next(item for item in finding.differences if item["field"] == "ratio.kind")
    assert kind == {"field": "ratio.kind", "expected": "population_ratio",
                    "actual": "average_ratio"}
    assert finding.summary == "比率类型（平均比率/总体比率）与口径字典不一致"


def test_average_ratio_matches_declared_average_metric(ratio_dictionary):
    finding = compare_report(
        "ratio.sql", parse_sql(AVERAGE_SQL, mode="fallback"), ratio_dictionary
    )[0]
    assert finding.category == "consistent"
    assert finding.evidence["observed"]["ratio"]["kind"] == "average_ratio"


def test_zero_division_policy_mismatch_is_reported(ratio_dictionary):
    sql = POPULATION_SQL.replace(
        "NULLIF(COUNT(DISTINCT order_id), 0)", "COUNT(DISTINCT order_id)")
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             ratio_dictionary)[0]
    assert finding.category == "drift"
    zero = next(item for item in finding.differences
                if item["field"] == "ratio.zero_division")
    assert zero["expected"] == "nullif"
    assert zero["actual"] == "unprotected"


def test_zero_division_policy_alias_is_normalised():
    metric = ratio_metric(ratio={
        "kind": "population_ratio",
        "numerator": {"aggregation": "sum", "field": "pay_amount"},
        "denominator": {"aggregation": "count_distinct", "field": "order_id"},
        "zero_division": "ifnull",
    })
    loaded = dictionary_from_document({"metrics": [metric]}).metrics[0]
    assert loaded.ratio.zero_division == "coalesce_zero"


def test_unsupported_denominator_is_undetermined_not_dropped(ratio_dictionary):
    sql = POPULATION_SQL.replace(
        "COUNT(DISTINCT order_id)", "percentile_approx(quantity, 0.5)")
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             ratio_dictionary)[0]
    assert finding.category == "undetermined"
    assert finding.metric == "avg_order_value"
    fields = {item["field"] for item in finding.differences}
    assert "ratio.undetermined_reason" in fields
    assert "ratio.denominator.expression" in fields
    assert finding.evidence["observed"]["ratio"]["denominator"]["supported"] is False
    assert "percentile_approx" in finding.evidence["observed"]["ratio"]["undetermined"][0]


def test_unsupported_ratio_is_undetermined_even_when_unregistered():
    dictionary = one_metric_dictionary()
    sql = """
    SELECT SUM(pay_amount) / NULLIF(udf(quantity), 0) AS mystery_ratio
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             dictionary)[0]
    assert finding.category == "unregistered"
    assert any(item["field"] == "ratio.undetermined_reason"
               for item in finding.differences)


def test_ratio_metric_compared_with_single_aggregate_is_drift():
    dictionary = one_metric_dictionary(
        source_fields=["order_paid_at", "order_status", "pay_amount"]
    )
    sql = """
    SELECT SUM(pay_amount) AS avg_order_value
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             dictionary)[0]
    assert finding.category == "drift"
    assert finding.differences[0] == {
        "field": "aggregation", "expected": "ratio", "actual": "sum",
    }


def test_single_metric_compared_with_ratio_is_drift(dictionary):
    sql = """
    SELECT SUM(pay_amount) / NULLIF(COUNT(order_status), 0) AS order_gmv
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    finding = compare_report("ratio.sql", parse_sql(sql, mode="fallback"),
                             dictionary)[0]
    assert finding.category == "drift"
    assert finding.differences[0] == {
        "field": "aggregation", "expected": "sum", "actual": "ratio",
    }


def test_undetermined_can_block_a_batch(ratio_dictionary):
    findings = ratio_findings(ratio_dictionary)
    assert failing_categories(findings, ["undetermined"]) == ["undetermined"]


@pytest.mark.parametrize("mode", ["fallback", "primary"])
def test_ratio_evidence_keeps_existing_audit_shape(ratio_dictionary, mode):
    findings = ratio_findings(ratio_dictionary, mode=mode)
    for finding in findings:
        assert finding.report.startswith("fixtures/ratio_reports/")
        assert finding.line_start >= 2
        assert finding.line_end >= finding.line_start
        assert finding.evidence["report"] == finding.report
        assert finding.evidence["line_range"] == [finding.line_start, finding.line_end]
        assert finding.evidence["parser_source"] == mode
        assert isinstance(finding.evidence["warnings"], list)
