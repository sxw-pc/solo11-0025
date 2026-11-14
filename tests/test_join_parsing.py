from __future__ import annotations

import pytest

from koujing.sql_parse import parse_fallback, parse_sql
from koujing.sql_parse.joins import extract_joins

pytest.importorskip("sqlglot", reason="primary parser parity requires sqlglot")
from koujing.sql_parse import parse_primary  # noqa: E402


def _join(sql, parser):
    parsed = parser(sql)
    assert len(parsed.joins) == 1
    return parsed.joins[0].to_dict()


def test_single_table_query_has_no_join_spec():
    sql = "SELECT SUM(amount) AS m FROM fact_order WHERE status = 'ok'"
    assert parse_fallback(sql).joins == ()
    assert parse_primary(sql).joins == ()


def test_inner_join_equality_endpoints_are_qualified_and_bound():
    sql = """
    SELECT SUM(o.amount) AS m
    FROM fact_order o INNER JOIN dim_shop s ON o.shop_id = s.shop_id
    GROUP BY o.shop_id
    """
    fallback = _join(sql, parse_fallback)
    primary = _join(sql, parse_primary)
    expected = {
        "join_type": "inner",
        "left_alias": "o",
        "left_table": "fact_order",
        "right_alias": "s",
        "right_table": "dim_shop",
        "equalities": [{
            "left": {"alias": "o", "table": "fact_order", "column": "shop_id"},
            "right": {"alias": "s", "table": "dim_shop", "column": "shop_id"},
        }],
        "decidable": True,
        "undecidable_reasons": [],
    }
    assert fallback == expected
    assert primary == expected


def test_bare_join_keyword_is_normalised_to_inner():
    sql = "SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k = b.k"
    assert _join(sql, parse_fallback)["join_type"] == "inner"
    assert _join(sql, parse_primary)["join_type"] == "inner"


def test_left_join_and_composite_key_keep_endpoint_bindings():
    sql = """
    SELECT SUM(o.amount) AS m FROM fact_order o
    LEFT JOIN dim_shop s ON s.shop_id = o.shop_id AND s.city_id = o.city_id
    """
    for parser in (parse_fallback, parse_primary):
        join = _join(sql, parser)
        assert join["join_type"] == "left"
        assert join["decidable"] is True
        # Reversed equality text still orients the declared-left endpoint first.
        assert [eq["left"]["column"] for eq in join["equalities"]] == ["city_id", "shop_id"]
        assert all(eq["left"]["table"] == "fact_order" for eq in join["equalities"])
        assert all(eq["right"]["table"] == "dim_shop" for eq in join["equalities"])


def test_equality_order_and_orientation_do_not_change_the_relationship():
    one = "SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k1 = b.k1 AND a.k2 = b.k2"
    two = "SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON b.k2 = a.k2 AND b.k1 = a.k1"
    for parser in (parse_fallback, parse_primary):
        assert _join(one, parser) == _join(two, parser)


@pytest.mark.parametrize("sql, reason", [
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k = b.k OR a.j = b.j",
     "or_in_join_predicate"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k > b.k",
     "non_equality_conjunct"),
    ("SELECT SUM(a.x) AS m FROM t1 a, t2 b WHERE a.k = b.k",
     "comma_or_cross_join"),
    ("SELECT SUM(a.x) AS m FROM t1 a CROSS JOIN t2 b",
     "comma_or_cross_join"),
    ("SELECT SUM(a.x) AS m FROM t1 a RIGHT JOIN t2 b ON a.k = b.k",
     "unsupported_join_type"),
    ("SELECT SUM(a.x) AS m FROM t1 a FULL JOIN t2 b ON a.k = b.k",
     "unsupported_join_type"),
    ("SELECT SUM(a.x) AS m FROM t1 a NATURAL JOIN t2 b",
     "unsupported_join_type"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b USING (k)",
     "missing_on_clause"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON upper(a.k) = b.k",
     "join_endpoint_is_not_a_column"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON k = b.k",
     "unqualified_column_in_on"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k = c.k",
     "column_alias_not_bound"),
    ("SELECT SUM(a.x) AS m FROM (SELECT * FROM t) a JOIN t2 b ON a.k = b.k",
     "subquery_in_from"),
    ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k1 = b.k1 JOIN t3 c ON c.k = a.k",
     "more_than_one_join"),
])
def test_undecidable_shapes_are_explicit_and_parity_holds(sql, reason):
    fallback = _join(sql, parse_fallback)
    primary = _join(sql, parse_primary)
    assert fallback["decidable"] is False
    assert primary["decidable"] is False
    assert reason in fallback["undecidable_reasons"]
    assert fallback == primary


def test_parenthesised_or_is_rejected_and_parity_holds():
    sql = ("SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b "
           "ON a.k1 = b.k1 AND (a.k2 = b.k2 OR a.k3 = b.k3)")
    fallback = _join(sql, parse_fallback)
    primary = _join(sql, parse_primary)
    assert fallback["undecidable_reasons"] == ["or_in_join_predicate"]
    assert fallback == primary


def test_parenthesised_equality_group_is_decidable():
    sql = "SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON ((a.k = b.k))"
    for parser in (parse_fallback, parse_primary):
        join = _join(sql, parser)
        assert join["decidable"] is True
        assert join["equalities"][0]["left"]["column"] == "k"


def test_joins_are_part_of_parser_comparable_contract():
    sql = ("SELECT SUM(o.amount) AS m FROM fact_order o "
           "LEFT JOIN dim_shop s ON o.shop_id = s.shop_id")
    assert parse_primary(sql).comparable()["joins"] == \
        parse_fallback(sql).comparable()["joins"]


def test_auto_mode_fallback_also_carries_joins():
    parsed = parse_sql(
        "SELECT SUM(a.x) AS m FROM t1 a JOIN t2 b ON a.k = b.k",
        mode="fallback",
    )
    assert parsed.joins[0].decidable is True
    assert extract_joins("") == ()
