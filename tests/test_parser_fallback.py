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


# ---------------------------------------------------------------------------
# CTE / derived-table lineage
# ---------------------------------------------------------------------------

CTE_SQL = """
WITH paid AS (
  SELECT pay_amount, order_paid_at, order_status
  FROM fact_order
)
SELECT DATE(p.order_paid_at) AS order_date,
       SUM(p.pay_amount) AS order_gmv
FROM paid p
WHERE p.order_status = 'paid'
GROUP BY DATE(p.order_paid_at);
"""

DERIVED_SQL = """
SELECT DATE(o.order_paid_at) AS order_date,
       SUM(o.gmv) AS order_gmv
FROM (
  SELECT pay_amount AS gmv, order_paid_at, order_status
  FROM fact_order
) o
WHERE o.order_status = 'paid'
GROUP BY DATE(o.order_paid_at);
"""


def test_cte_lineage_traces_aggregate_back_to_physical_table():
    parsed = parse_fallback(CTE_SQL)
    assert parsed.tables == ("fact_order",)
    assert [node.alias for node in parsed.ctes] == ["paid"]
    assert parsed.ctes[0].physical_tables == ("fact_order",)
    relation = parsed.relations[0]
    assert relation.kind == "cte"
    assert relation.alias == "p"
    assert relation.name == "paid"
    assert relation.physical_tables == ("fact_order",)
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("fact_order",)
    assert [(hop.table, hop.column, hop.kind) for hop in lineage.chain] == [
        ("p", "pay_amount", "cte"),
        ("fact_order", "pay_amount", "physical"),
    ]


def test_cte_scope_is_recorded_in_comparable_contract():
    parsed = parse_fallback(CTE_SQL)
    comparable = parsed.comparable()
    assert comparable["tables"] == ["fact_order"]
    assert comparable["ctes"][0]["kind"] == "cte"
    assert comparable["aggregates"][0]["lineage"][0]["chain"][0]["kind"] == "cte"


def test_derived_table_lineage_traces_aggregate_back_to_physical_table():
    parsed = parse_fallback(DERIVED_SQL)
    # The derived alias `o` must never participate as a source table.
    assert parsed.tables == ("fact_order",)
    relation = parsed.relations[0]
    assert relation.kind == "derived"
    assert relation.alias == "o"
    assert relation.physical_tables == ("fact_order",)
    assert relation.children[0].kind == "table"
    assert relation.children[0].name == "fact_order"
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("fact_order",)
    assert [(hop.table, hop.column, hop.kind) for hop in lineage.chain] == [
        ("o", "gmv", "derived"),
        ("fact_order", "pay_amount", "physical"),
    ]


def test_derived_table_alias_is_not_treated_as_a_source_table():
    parsed = parse_fallback(DERIVED_SQL)
    assert "o" not in parsed.tables
    assert all(node.kind != "table" or node.name != "o" for node in parsed.relations)


def test_field_lineage_uses_physical_column_names_through_derived_table():
    parsed = parse_fallback(DERIVED_SQL)
    gmv_lineage = next(item for item in parsed.field_lineage if item.column == "gmv")
    assert gmv_lineage.physical_tables == ("fact_order",)
    assert [hop.column for hop in gmv_lineage.chain] == ["gmv", "pay_amount"]
    # The bottom-level column is the physical fact_order column name.
    assert "pay_amount" in parsed.select_fields


def test_nested_derived_tables_build_a_full_chain():
    sql = """
    SELECT SUM(x.pay_amount) AS order_gmv FROM (
      SELECT o.pay_amount FROM (
        SELECT pay_amount, order_status FROM fact_order
      ) o
      WHERE o.order_status = 'paid'
    ) x;
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("fact_order",)
    lineage = parsed.aggregates[0].lineage[0]
    assert [(hop.table, hop.kind) for hop in lineage.chain] == [
        ("x", "derived"),
        ("o", "derived"),
        ("fact_order", "physical"),
    ]


def test_derived_alias_shadowing_a_physical_table_name_stays_scoped():
    sql = """
    SELECT SUM(fact_order.pay_amount) AS order_gmv
    FROM (SELECT pay_amount FROM fact_order) fact_order;
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("fact_order",)
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("fact_order",)
    assert [(hop.table, hop.kind) for hop in lineage.chain] == [
        ("fact_order", "derived"),
        ("fact_order", "physical"),
    ]
    assert any("shadows a physical table name" in warning for warning in parsed.warnings)


def test_cte_shadowing_a_physical_table_name_stays_scoped():
    sql = """
    WITH fact_order AS (SELECT pay_amount FROM fact_order_archive)
    SELECT SUM(pay_amount) AS order_gmv FROM fact_order;
    """
    parsed = parse_fallback(sql)
    # The CTE name wins inside the statement; its body reads the archive.
    assert parsed.tables == ("fact_order_archive",)
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("fact_order_archive",)
    assert [(hop.table, hop.kind) for hop in lineage.chain] == [
        ("fact_order", "cte"),
        ("fact_order_archive", "physical"),
    ]


def test_recursive_cte_self_reference_terminates_without_looping():
    sql = """
    WITH RECURSIVE tree AS (
      SELECT id, parent_id FROM fact_node WHERE parent_id IS NULL
      UNION ALL
      SELECT n.id, n.parent_id
      FROM fact_node n JOIN tree t ON n.parent_id = t.id
    )
    SELECT COUNT(DISTINCT id) AS node_count FROM tree;
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("fact_node",)
    lineage_entries = parsed.aggregates[0].lineage
    # The anchor branch reaches the physical table.
    assert any(entry.physical_tables == ("fact_node",) for entry in lineage_entries)
    # The self-referential branch terminates at an unresolved hop instead of
    # looping forever.
    chains = [hop.kind for entry in lineage_entries for hop in entry.chain]
    assert "unresolved" in chains
    assert any("recursive CTE" in warning for warning in parsed.warnings)


def test_scalar_subquery_sources_are_physical_tables():
    sql = """
    SELECT SUM(pay_amount) AS order_gmv
    FROM fact_order
    WHERE user_id IN (SELECT user_id FROM dim_account WHERE status = 'ok');
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("dim_account", "fact_order")
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("fact_order",)
    nested = next(rel for rel in parsed.relations if rel.kind == "derived")
    assert nested.physical_tables == ("dim_account",)


def test_join_derived_table_resolves_qualified_reference():
    sql = """
    SELECT COUNT(DISTINCT o.user_id) AS active_user
    FROM fact_login l
    JOIN (SELECT user_id FROM dim_account) o ON o.user_id = l.user_id
    WHERE l.event_type = 'login';
    """
    parsed = parse_fallback(sql)
    assert parsed.tables == ("dim_account", "fact_login")
    lineage = parsed.aggregates[0].lineage[0]
    assert lineage.physical_tables == ("dim_account",)
    assert [(hop.table, hop.kind) for hop in lineage.chain] == [
        ("o", "derived"),
        ("dim_account", "physical"),
    ]
