"""Regressions for multi-aggregate attribution and alias normalisation.

A single report often carries several aggregates whose aliases map to
different dictionary metrics. Each aggregate must be checked in isolation:
its observed field set is its own arguments plus report-level dimensions,
filter columns and GROUP BY columns -- never a sibling aggregate's arguments.
"""

from __future__ import annotations

import sys

import pytest

from koujing.compare import compare_report, summarise
from koujing.sql_parse import parse_primary, parse_sql
from koujing.sql_parse.model import normalise_alias

SAME_TABLE_REPORT = """
SELECT
  DATE(order_paid_at) AS order_date,
  SUM(pay_amount) AS order_gmv,
  COUNT(DISTINCT order_id) AS paid_order_count
FROM fact_order
WHERE order_status = 'paid'
GROUP BY DATE(order_paid_at);
"""


def _findings(sql, dictionary, mode="fallback"):
    return compare_report("multi.sql", parse_sql(sql, mode=mode), dictionary)


def test_two_aggregates_on_one_table_are_checked_independently(dictionary):
    findings = _findings(SAME_TABLE_REPORT, dictionary)
    assert [(item.metric, item.category) for item in findings] == [
        ("order_gmv", "consistent"),
        ("paid_order_count", "consistent"),
    ]
    for finding in findings:
        assert finding.aggregate_index in {0, 1}
        fields = finding.evidence["observed"]["source_fields"]
        assert fields == sorted(set(fields))


def test_sibling_aggregate_arguments_do_not_leak(dictionary):
    findings = {item.metric: item for item in _findings(SAME_TABLE_REPORT, dictionary)}
    gmv_fields = set(findings["order_gmv"].evidence["observed"]["source_fields"])
    count_fields = set(findings["paid_order_count"].evidence["observed"]["source_fields"])
    assert "paid_order_count" not in " ".join(gmv_fields)
    assert "order_id" not in gmv_fields
    assert "pay_amount" not in count_fields
    assert gmv_fields == {"pay_amount", "order_paid_at", "order_status"}
    assert count_fields == {"order_id", "order_paid_at", "order_status"}


def test_dimension_columns_are_shared_but_not_aggregate_arguments(dictionary):
    findings = {item.metric: item for item in _findings(SAME_TABLE_REPORT, dictionary)}
    for finding in findings.values():
        assert "order_paid_at" in finding.evidence["observed"]["source_fields"]
        assert finding.evidence["observed"]["dimension_fields"] == ["order_paid_at"]


def test_mixed_categories_are_attributed_to_the_right_alias(dictionary):
    sql = """
    SELECT
      DATE(order_paid_at) AS order_date,
      SUM(pay_amount) AS order_gmv,
      SUM(coupon_amount) AS coupon_total
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    findings = {item.metric: item for item in _findings(sql, dictionary)}
    assert findings["order_gmv"].category == "consistent"
    assert findings["coupon_total"].category == "unregistered"
    # The unregistered sibling must not inherit order_gmv's argument fields.
    assert findings["coupon_total"].evidence["observed"]["source_fields"] == [
        "coupon_amount", "order_paid_at", "order_status",
    ]


@pytest.mark.parametrize("alias_sql", ["`Order_GMV`", '"Order_GMV"', "Order_GMV", "ORDER_GMV"])
def test_case_and_quoted_aliases_lookup_the_same_metric(dictionary, alias_sql):
    sql = f"""
    SELECT DATE(order_paid_at) AS order_date, SUM(pay_amount) AS {alias_sql}
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    findings = _findings(sql, dictionary)
    assert [(item.metric, item.category) for item in findings] == [("order_gmv", "consistent")]
    assert findings[0].evidence["aggregate_alias"] == "order_gmv"


def test_duplicate_alias_keeps_every_projection(dictionary):
    sql = """
    SELECT
      DATE(order_paid_at) AS order_date,
      SUM(pay_amount) AS order_gmv,
      SUM(coupon_amount) AS `ORDER_GMV`
    FROM fact_order
    WHERE order_status = 'paid'
    GROUP BY DATE(order_paid_at);
    """
    parsed = parse_sql(sql, mode="fallback")
    assert [item.alias for item in parsed.aggregates] == ["order_gmv", "order_gmv"]
    assert any("duplicate aggregate alias" in warning for warning in parsed.warnings)

    findings = compare_report("dup.sql", parsed, dictionary)
    assert len(findings) == 2
    assert [item.aggregate_index for item in findings] == [0, 1]
    assert [item.category for item in findings] == ["consistent", "missing_field"]
    declared = next(item for item in findings[1].differences
                    if item["field"] == "source_fields")
    assert declared["undeclared_in_report"] == ["coupon_amount"]


def test_duplicate_alias_is_not_collapsed_in_archived_output(dictionary):
    sql = """
    SELECT SUM(pay_amount) AS order_gmv, SUM(coupon_amount) AS Order_GMV
    FROM fact_order WHERE order_status = 'paid' GROUP BY DATE(order_paid_at);
    """
    findings = _findings(sql, dictionary)
    payload = [item.to_dict() for item in findings]
    assert len(payload) == 2
    assert [item["aggregate_index"] for item in payload] == [0, 1]
    assert payload[0] != payload[1]


def test_alias_normalisation_is_shared_by_both_parsers():
    for raw in ("`Order_GMV`", '"Order_GMV"', "[Order_GMV]", "'Order_GMV'", "Order_GMV", " order_gmv "):
        assert normalise_alias(raw) == "order_gmv"
    assert normalise_alias("") == ""


@pytest.mark.parametrize("sql", [
    SAME_TABLE_REPORT,
    'SELECT DATE(order_paid_at) AS order_date, SUM(pay_amount) AS "Order_GMV"\n'
    "FROM fact_order WHERE order_status = 'paid' GROUP BY DATE(order_paid_at);",
    """
    SELECT SUM(pay_amount) AS order_gmv, SUM(pay_amount) AS ORDER_GMV
    FROM fact_order WHERE order_status = 'paid' GROUP BY DATE(order_paid_at);
    """,
])
def test_parsers_normalise_multi_aggregate_reports_to_same_structure(dictionary, sql):
    pytest.importorskip("sqlglot")
    primary = parse_primary(sql)
    fallback = parse_sql(sql, mode="fallback")
    assert primary.comparable() == fallback.comparable()
    assert primary.dimension_fields == fallback.dimension_fields
    assert summarise(compare_report("multi.sql", primary, dictionary)) == \
        summarise(compare_report("multi.sql", fallback, dictionary))


def test_auto_mode_without_sqlglot_matches_forced_fallback(dictionary, monkeypatch):
    reports = {
        "same_table": SAME_TABLE_REPORT,
        "quoted_alias": 'SELECT DATE(order_paid_at) AS d, SUM(pay_amount) AS "Order_GMV"\n'
        "FROM fact_order WHERE order_status = 'paid' GROUP BY DATE(order_paid_at);",
        "duplicate_alias": "SELECT SUM(pay_amount) AS order_gmv, "
        "SUM(coupon_amount) AS ORDER_GMV FROM fact_order "
        "WHERE order_status = 'paid' GROUP BY DATE(order_paid_at);",
    }
    forced = {
        name: [(item.category, item.metric, item.aggregate_index)
               for item in compare_report(name, parse_sql(sql, mode="fallback"), dictionary)]
        for name, sql in reports.items()
    }
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    auto = {
        name: [(item.category, item.metric, item.aggregate_index)
               for item in compare_report(name, parse_sql(sql, mode="auto"), dictionary)]
        for name, sql in reports.items()
    }
    assert auto == forced
