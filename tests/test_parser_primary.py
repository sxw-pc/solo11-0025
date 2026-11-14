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


@pytest.mark.parametrize("name", [
    "ok_order_gmv_cte.sql",
    "ok_order_gmv_derived.sql",
    "drift_order_gmv_derived.sql",
])
def test_primary_and_fallback_agree_on_lineage_fixtures(name):
    sql = read_report(name)
    primary = parse_primary(sql)
    fallback = parse_sql(sql, mode="fallback")
    assert primary.comparable() == fallback.comparable()


@pytest.mark.parametrize("sql", [
    # CTE with a renamed projection.
    "WITH paid AS (SELECT pay_amount AS gmv FROM fact_order) "
    "SELECT SUM(p.gmv) AS order_gmv FROM paid p;",
    # Two CTEs joined inside the statement body.
    "WITH a AS (SELECT id, amt FROM fact_a), "
    "b AS (SELECT id FROM fact_b) "
    "SELECT SUM(a.amt) AS m FROM a JOIN b ON a.id = b.id;",
    # Nested FROM subqueries.
    "SELECT SUM(x.amt) AS m FROM ("
    "SELECT o.amt FROM (SELECT amt FROM fact_a) o) x;",
    # Derived alias shadowing the physical table name.
    "SELECT SUM(fact_order.amt) AS m FROM (SELECT amt FROM fact_a) fact_order;",
    # CTE name shadowing a physical table name.
    "WITH fact_a AS (SELECT amt FROM fact_b) SELECT SUM(amt) AS m FROM fact_a;",
    # Recursive CTE self reference.
    "WITH RECURSIVE r AS ("
    "SELECT id FROM fact_node WHERE parent_id IS NULL "
    "UNION ALL SELECT n.id FROM fact_node n JOIN r ON n.parent_id = r.id) "
    "SELECT COUNT(DISTINCT id) AS c FROM r;",
    # Scalar subquery in WHERE.
    "SELECT SUM(amt) AS m FROM fact_a WHERE id IN (SELECT id FROM fact_b);",
    # Star expansion inside a derived table.
    "SELECT SUM(o.amt) AS m FROM (SELECT * FROM fact_a) o;",
])
def test_primary_and_fallback_agree_on_lineage_shapes(sql):
    primary = parse_primary(sql)
    fallback = parse_sql(sql, mode="fallback")
    assert primary.comparable() == fallback.comparable()


def test_primary_recursive_cte_is_guarded_and_annotated():
    sql = (
        "WITH RECURSIVE r AS ("
        "SELECT id FROM fact_node "
        "UNION ALL SELECT n.id FROM fact_node n JOIN r ON n.parent_id = r.id) "
        "SELECT COUNT(DISTINCT id) AS c FROM r;"
    )
    parsed = parse_primary(sql)
    assert parsed.tables == ("fact_node",)
    assert any("recursive CTE" in warning for warning in parsed.warnings)
    entries = parsed.aggregates[0].lineage
    kinds = [hop.kind for entry in entries for hop in entry.chain]
    assert "physical" in kinds
    assert "unresolved" in kinds
