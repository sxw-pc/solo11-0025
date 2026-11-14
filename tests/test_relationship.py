from __future__ import annotations

import pytest

from conftest import valid_metric
from koujing.compare import compare_report
from koujing.dict_load import dictionary_from_document
from koujing.relationship import (
    PROVABLE,
    UNDECIDABLE,
    UNPROVABLE,
    evaluate_join,
)
from koujing.sql_parse import parse_sql


def make_dictionary(contract=None, **metric_overrides):
    metric = valid_metric(
        metric="shop_gmv",
        source_tables=["fact_order", "dim_shop"],
        source_fields=["pay_amount", "shop_id", "city_id", "order_paid_at"],
        aggregation="sum",
        filter_expr="",
        granularity="day",
    )
    metric.update(metric_overrides)
    if contract is not None:
        metric["join_contract"] = contract
    return dictionary_from_document({"metrics": [metric]})


COMPOSITE_CONTRACT = {
    "join_type": "left",
    "left_table": "fact_order",
    "right_table": "dim_shop",
    "on": [
        {"left_column": "shop_id", "right_column": "shop_id"},
        {"left_column": "city_id", "right_column": "city_id"},
    ],
    "right_unique_key": ["shop_id", "city_id"],
}

SELECT_TEMPLATE = """
SELECT DATE(o.order_paid_at) AS d, s.shop_id, s.city_id,
       SUM(o.pay_amount) AS shop_gmv
FROM {from_clause}
GROUP BY DATE(o.order_paid_at), s.shop_id, s.city_id
"""


def parse(from_clause, mode="fallback"):
    return parse_sql(SELECT_TEMPLATE.format(from_clause=from_clause), mode=mode)


def verdict_of(dictionary, from_clause, mode="fallback"):
    parsed = parse(from_clause, mode)
    metric = dictionary.metrics[0]
    return evaluate_join(parsed, metric.join_contract), parsed


def test_full_composite_key_is_provable_many_to_one():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id",
    )
    assert verdict.status == PROVABLE
    assert verdict.provable is True
    assert verdict.reasons == ()
    # The provable wording must not claim runtime uniqueness.
    assert "不代表运行时数据已唯一" in verdict.detail


def test_reversed_equality_and_reordered_conjuncts_remain_equivalent():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    canonical, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id",
    )
    reversed_sql, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON s.city_id = o.city_id AND s.shop_id = o.shop_id",
    )
    assert reversed_sql.status == canonical.status == PROVABLE
    assert reversed_sql.actual_pairs == canonical.actual_pairs


def test_missing_key_component_is_not_provable_with_evidence():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s ON o.shop_id = s.shop_id",
    )
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("missing_key_pair",)
    assert verdict.missing_pairs == ({
        "left_table": "fact_order",
        "left_column": "city_id",
        "right_table": "dim_shop",
        "right_column": "city_id",
    },)


def test_wrong_join_column_is_distinguished_from_missing_key():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.region_id",
    )
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("join_column_mismatch",)
    mismatch = verdict.mismatched_pairs[0]
    assert mismatch["expected"]["right_column"] == "city_id"
    assert mismatch["actual"]["right_column"] == "region_id"


def test_same_field_set_with_different_pairing_can_change_the_verdict():
    # Both queries touch exactly shop_id/city_id on both tables, but pairing
    # shop_id to city_id crosses the columns and breaks the declared contract.
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    correct, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id",
    )
    crossed, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.city_id AND o.city_id = s.shop_id",
    )
    assert correct.status == PROVABLE
    assert crossed.status == UNPROVABLE
    assert crossed.reasons == ("join_column_mismatch",)


def test_extra_equality_pair_does_not_invalidate_many_to_one_proof():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id AND o.tag = s.tag",
    )
    assert verdict.status == PROVABLE
    assert verdict.extra_pairs == ({
        "left_table": "fact_order",
        "left_column": "tag",
        "right_table": "dim_shop",
        "right_column": "tag",
    },)


def test_unique_key_component_not_covered_by_the_join_is_rejected():
    from koujing.dict_load import JoinContract, JoinKeyPair
    from koujing.relationship import evaluate_join

    def contract_with_key(key_columns):
        return JoinContract(
            join_type="inner",
            left_table="fact_order",
            right_table="dim_shop",
            on=(JoinKeyPair("fact_order", "shop_id", "dim_shop", "shop_id"),),
            right_unique_key=tuple(sorted(key_columns)),
        )

    parsed = parse_sql(
        "SELECT SUM(o.pay_amount) AS shop_gmv FROM fact_order o "
        "INNER JOIN dim_shop s ON o.shop_id = s.shop_id",
        mode="fallback",
    )
    assert evaluate_join(parsed, contract_with_key(["shop_id"])).status == PROVABLE

    # A second declared key component without a matching right-side endpoint
    # means the join cannot be proved many-to-one.
    verdict = evaluate_join(parsed, contract_with_key(["shop_id", "city_id"]))
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("unique_key_component_not_covered",)
    assert verdict.uncovered_key_columns == ("city_id",)


def test_preserved_side_mismatch_is_reported_for_left_join():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "dim_shop s LEFT JOIN fact_order o "
        "ON s.shop_id = o.shop_id AND s.city_id = o.city_id",
    )
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("preserved_side_mismatch",)


def test_join_type_mismatch_is_reported():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(
        dictionary,
        "fact_order o INNER JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id",
    )
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("join_type_mismatch",)
    assert verdict.expected_join_type == "left"
    assert verdict.actual_join_type == "inner"


@pytest.mark.parametrize("from_clause, parser_reason", [
    ("fact_order o LEFT JOIN dim_shop s "
     "ON o.shop_id = s.shop_id OR o.city_id = s.city_id", "or_in_join_predicate"),
    ("fact_order o LEFT JOIN dim_shop s "
     "ON o.shop_id = s.shop_id AND o.city_id >= s.city_id", "non_equality_conjunct"),
    ("fact_order o, dim_shop s", "comma_or_cross_join"),
])
def test_undecidable_syntax_never_counts_as_safe(from_clause, parser_reason):
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    verdict, _ = verdict_of(dictionary, from_clause)
    assert verdict.status == UNDECIDABLE
    assert verdict.provable is False
    assert verdict.reasons == ("join_not_within_bounded_syntax",)
    assert parser_reason in verdict.parser_reasons


def test_metric_without_contract_skips_relationship_check():
    dictionary = make_dictionary(None)
    parsed = parse(
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id"
    )
    assert evaluate_join(parsed, dictionary.metrics[0].join_contract) is None


def test_contract_on_single_table_query_is_not_provable():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    parsed = parse_sql(
        "SELECT DATE(order_paid_at) AS d, SUM(pay_amount) AS shop_gmv "
        "FROM fact_order GROUP BY DATE(order_paid_at)",
        mode="fallback",
    )
    verdict = evaluate_join(parsed, dictionary.metrics[0].join_contract)
    assert verdict.status == UNPROVABLE
    assert verdict.reasons == ("query_has_no_join",)


def test_failed_contract_is_drift_with_evidence_in_finding():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    parsed = parse(
        "fact_order o LEFT JOIN dim_shop s ON o.shop_id = s.shop_id"
    )
    finding = next(
        item for item in compare_report("reports/r.sql", parsed, dictionary)
        if item.metric == "shop_gmv"
    )
    assert finding.category == "drift"
    difference = next(item for item in finding.differences
                      if item["field"] == "join_contract")
    assert difference["verdict"] == UNPROVABLE
    assert difference["reasons"] == ["missing_key_pair"]
    verdict = finding.evidence["join_verdict"]
    assert verdict["status"] == UNPROVABLE
    assert verdict["expected_pairs"] == finding.evidence["expected"]["join_contract"]["on"]
    assert verdict["actual_pairs"] == difference["actual"]["pairs"]
    assert finding.evidence["observed_joins"][0]["decidable"] is True


def test_provable_contract_keeps_consistent_category():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    parsed = parse(
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id"
    )
    finding = next(
        item for item in compare_report("reports/r.sql", parsed, dictionary)
        if item.metric == "shop_gmv"
    )
    assert finding.category == "consistent"
    assert finding.evidence["join_verdict"]["status"] == PROVABLE
    assert all(item["field"] != "join_contract" for item in finding.differences)


def test_undecidable_contract_is_drift_not_consistent():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    parsed = parse(
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id OR o.city_id = s.city_id"
    )
    finding = next(
        item for item in compare_report("reports/r.sql", parsed, dictionary)
        if item.metric == "shop_gmv"
    )
    assert finding.category == "drift"
    assert "无法证明为多对一" in finding.summary
    assert finding.evidence["join_verdict"]["status"] == UNDECIDABLE


def test_legacy_metric_finding_has_no_join_verdict_evidence():
    dictionary = make_dictionary(None)
    parsed = parse(
        "fact_order o LEFT JOIN dim_shop s "
        "ON o.shop_id = s.shop_id AND o.city_id = s.city_id"
    )
    finding = next(
        item for item in compare_report("reports/r.sql", parsed, dictionary)
        if item.metric == "shop_gmv"
    )
    assert "join_verdict" not in finding.evidence
    assert all(item["field"] != "join_contract" for item in finding.differences)


def test_missing_field_category_still_takes_priority_over_join_drift():
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    # An undeclared column (coupon_amount) coexists with an incomplete join.
    sql = """
    SELECT DATE(o.order_paid_at) AS d, o.coupon_amount,
           SUM(o.pay_amount) AS shop_gmv
    FROM fact_order o LEFT JOIN dim_shop s ON o.shop_id = s.shop_id
    GROUP BY DATE(o.order_paid_at), o.coupon_amount
    """
    finding = next(
        item for item in compare_report("reports/r.sql", parse_sql(sql, mode="fallback"),
                                        dictionary)
        if item.metric == "shop_gmv"
    )
    assert finding.category == "missing_field"
    # The relationship evidence is still retained for audit.
    assert finding.evidence["join_verdict"]["status"] == UNPROVABLE
    assert any(item["field"] == "join_contract" for item in finding.differences)


def test_primary_and_fallback_verdict_agree():
    pytest.importorskip("sqlglot")
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    clause = (
        "fact_order o LEFT JOIN dim_shop s "
        "ON s.city_id = o.city_id AND s.shop_id = o.shop_id"
    )
    fallback = evaluate_join(parse(clause, "fallback"),
                             dictionary.metrics[0].join_contract)
    primary = evaluate_join(parse(clause, "primary"),
                            dictionary.metrics[0].join_contract)
    assert primary.to_dict() == fallback.to_dict()
    assert primary.status == PROVABLE


def test_verdict_is_deterministic_and_serialises_to_json():
    import json

    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    clause = "fact_order o LEFT JOIN dim_shop s ON o.shop_id = s.shop_id"
    first, _ = verdict_of(dictionary, clause)
    second, _ = verdict_of(dictionary, clause)
    assert first.to_dict() == second.to_dict()
    rendered = json.dumps(first.to_dict(), ensure_ascii=False)
    assert "missing_key_pair" in rendered


@pytest.mark.parametrize("from_clause, expected_status, expected_reason", [
    ("fact_order o LEFT JOIN dim_shop s "
     "ON o.shop_id = s.shop_id AND o.city_id = s.city_id", PROVABLE, ()),
    ("fact_order o LEFT JOIN dim_shop s ON o.shop_id = s.shop_id",
     UNPROVABLE, ("missing_key_pair",)),
    ("fact_order o LEFT JOIN dim_shop s "
     "ON o.shop_id = s.shop_id AND o.city_id = s.region_id",
     UNPROVABLE, ("join_column_mismatch",)),
    ("fact_order o LEFT JOIN dim_shop s "
     "ON o.shop_id = s.shop_id OR o.city_id = s.city_id",
     UNDECIDABLE, ("join_not_within_bounded_syntax",)),
    ("dim_shop s LEFT JOIN fact_order o "
     "ON s.shop_id = o.shop_id AND s.city_id = o.city_id",
     UNPROVABLE, ("preserved_side_mismatch",)),
])
def test_primary_and_fallback_produce_identical_verdicts(
    from_clause, expected_status, expected_reason
):
    pytest.importorskip("sqlglot")
    dictionary = make_dictionary(COMPOSITE_CONTRACT)
    contract = dictionary.metrics[0].join_contract
    fallback = evaluate_join(parse(from_clause, "fallback"), contract)
    primary = evaluate_join(parse(from_clause, "primary"), contract)
    assert fallback.status == primary.status == expected_status
    assert fallback.reasons == primary.reasons == expected_reason
    assert fallback.to_dict() == primary.to_dict()
