from __future__ import annotations

import pytest

from koujing.sql_parse import parse_fallback, parse_sql
from koujing.sql_parse.ratio import parse_ratio_expression


def test_ratio_extraction_population_ratio():
    ratio = parse_ratio_expression("SUM(pay_amount) / NULLIF(COUNT(order_id), 0)")
    assert ratio.kind == "population_ratio"
    assert ratio.zero_division == "nullif"
    assert ratio.supported
    assert (ratio.numerator.aggregation, ratio.numerator.expression) == ("sum", "pay_amount")
    assert (ratio.denominator.aggregation, ratio.denominator.expression) == (
        "count", "order_id")


def test_ratio_extraction_keeps_avg_of_division_distinct():
    average = parse_ratio_expression("AVG(x / NULLIF(y, 0))")
    population = parse_ratio_expression("AVG(x) / NULLIF(AVG(y), 0)")
    assert average.kind == "average_ratio"
    assert population.kind == "population_ratio"
    assert average != population


def test_ratio_extraction_coalesce_zero_policy():
    ratio = parse_ratio_expression("COALESCE(SUM(x) / NULLIF(SUM(y), 0), 0)")
    assert ratio.zero_division == "coalesce_zero"
    assert ratio.supported


def test_ratio_extraction_ignores_single_and_nested_aggregates():
    assert parse_ratio_expression("SUM(pay_amount)") is None
    assert parse_ratio_expression("SUM(x / y)") is None
    assert parse_ratio_expression("COUNT(DISTINCT user_id)") is None


def test_ratio_extraction_marks_unsupported_operand():
    ratio = parse_ratio_expression("SUM(x) / udf(y)")
    assert not ratio.supported
    assert ratio.denominator.aggregation == ""
    assert ratio.denominator.expression == "udf(y)"
    assert any("udf" in reason for reason in ratio.unsupported)


def test_ratio_extraction_marks_subquery_denominator():
    ratio = parse_ratio_expression("SUM(x) / (SELECT COUNT(*) FROM t2)")
    assert not ratio.supported
    assert any("subquery" in reason for reason in ratio.unsupported)


def test_ratio_extraction_marks_multiple_divisions():
    ratio = parse_ratio_expression("SUM(x) / SUM(y) / SUM(z)")
    assert not ratio.supported


def test_fallback_wrapper_keeps_ratio_candidate():
    parsed = parse_fallback(
        "SELECT SUM(x) / NULLIF(COUNT(order_id), 0) AS r FROM t;"
    )
    aggregate = parsed.aggregates[0]
    assert aggregate.function == "ratio"
    assert aggregate.alias == "r"
    assert aggregate.ratio.kind == "population_ratio"
    assert aggregate.fields == ("order_id", "x")


def test_fallback_does_not_misread_divided_aggregates():
    parsed = parse_fallback("SELECT SUM(x) / SUM(y) AS r FROM t;")
    aggregate = parsed.aggregates[0]
    assert aggregate.function == "ratio"
    assert aggregate.ratio.numerator.expression == "x"
    assert aggregate.ratio.denominator.expression == "y"


def test_fallback_keeps_legacy_avg_metric():
    parsed = parse_fallback("SELECT AVG(amount) AS sample_metric FROM fact_sample;")
    aggregate = parsed.aggregates[0]
    assert aggregate.function == "avg"
    assert aggregate.ratio is None


def test_ratio_projection_without_alias_is_still_extracted():
    parsed = parse_fallback("SELECT SUM(x) / NULLIF(SUM(y), 0) FROM t;")
    assert parsed.aggregates[0].function == "ratio"
    assert parsed.aggregates[0].alias == ""


@pytest.mark.parametrize("sql", [
    "SELECT SUM(pay_amount) / NULLIF(COUNT(DISTINCT order_id), 0) AS r "
    "FROM fact_order GROUP BY DATE(order_paid_at);",
    "SELECT AVG(pay_amount / NULLIF(quantity, 0)) AS r "
    "FROM fact_order GROUP BY DATE(order_paid_at);",
    "SELECT AVG(x) / NULLIF(AVG(y), 0) AS r FROM t;",
    "SELECT COALESCE(SUM(x) / NULLIF(SUM(y), 0), 0) AS r FROM t;",
    "SELECT SUM(x) / NULLIF(udf(y), 0) AS r FROM t;",
])
def test_both_parsers_expose_same_ratio_subset(sql):
    pytest.importorskip("sqlglot")
    primary = parse_sql(sql, mode="primary")
    fallback = parse_sql(sql, mode="fallback")
    assert primary.comparable() == fallback.comparable()
