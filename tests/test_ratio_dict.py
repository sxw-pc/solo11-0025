from __future__ import annotations

import pytest

from conftest import DICT_RATIO, load_dictionary, valid_metric
from koujing.config import canonical_json
from koujing.dict_load import DictValidationError, dictionary_from_document


def errors_for(document):
    with pytest.raises(DictValidationError) as caught:
        dictionary_from_document(document)
    return caught.value.errors


def ratio_metric(**overrides):
    metric = valid_metric(
        aggregation="ratio",
        ratio={
            "kind": "population_ratio",
            "numerator": {"aggregation": "sum", "field": "amount"},
            "denominator": {"aggregation": "count", "field": "order_id"},
            "zero_division": "nullif",
        },
    )
    metric.update(overrides)
    return metric


def test_ratio_dictionary_fixture_roundtrip(ratio_dictionary):
    assert ratio_dictionary.metric_names() == (
        "avg_basket_value", "avg_order_value", "order_gmv", "paid_order_count"
    )
    order_value = ratio_dictionary.by_name()["avg_order_value"]
    assert order_value.is_ratio
    assert order_value.aggregation == "ratio"
    assert order_value.ratio.kind == "population_ratio"
    assert order_value.ratio.numerator.aggregation == "sum"
    assert order_value.ratio.numerator.field == "pay_amount"
    assert order_value.ratio.denominator.aggregation == "count_distinct"
    assert order_value.ratio.denominator.field == "order_id"
    assert order_value.ratio.zero_division == "nullif"
    again = load_dictionary(DICT_RATIO, yaml_backend="lite")
    assert again.fingerprint == ratio_dictionary.fingerprint
    assert again.metrics == ratio_dictionary.metrics


def test_legacy_metric_payload_has_no_ratio_key_and_stable_fingerprint(dictionary):
    for metric in dictionary.metrics:
        assert metric.ratio is None
        assert "ratio" not in metric.to_dict()
    plain = dictionary_from_document({
        "metrics": [valid_metric(aggregation="sum", filter_expr="status = 'ok'")]
    })
    payload = plain.metrics[0].to_dict()
    assert "ratio" not in payload
    assert "ratio" not in canonical_json(payload)


def test_ratio_operands_are_required():
    metric = ratio_metric()
    metric["ratio"] = {
        "kind": "population_ratio",
        "numerator": {"aggregation": "sum"},
        "zero_division": "nullif",
    }
    errors = errors_for({"metrics": [metric]})
    codes = {(error["code"], error["field"]) for error in errors}
    assert ("missing_field", "ratio.numerator.field") in codes
    assert ("invalid_ratio_operand", "ratio.denominator") in codes


def test_ratio_zero_division_policy_is_validated_and_normalised():
    errors = errors_for({"metrics": [ratio_metric(ratio={
        "numerator": {"aggregation": "sum", "field": "amount"},
        "denominator": {"aggregation": "count", "field": "order_id"},
        "zero_division": "explode",
    })]})
    assert any(error["code"] == "invalid_zero_division" for error in errors)
    assert any(error["field"] == "ratio.zero_division" for error in errors)


def test_ratio_kind_is_validated():
    errors = errors_for({"metrics": [ratio_metric(ratio={
        "kind": "geometric_ratio",
        "numerator": {"aggregation": "sum", "field": "amount"},
        "denominator": {"aggregation": "count", "field": "order_id"},
    })]})
    assert any(error["code"] == "invalid_ratio_kind" for error in errors)


def test_ratio_kind_and_zero_aliases_are_normalised():
    metric = ratio_metric(ratio={
        "kind": "avg",
        "numerator": {"aggregation": "avg", "field": "amount"},
        "denominator": {"aggregation": "avg", "field": "order_id"},
        "zero_division": "safe_divide",
    })
    loaded = dictionary_from_document({"metrics": [metric]}).metrics[0]
    assert loaded.ratio.kind == "average_ratio"
    assert loaded.ratio.zero_division == "nullif"


def test_average_ratio_requires_row_operands():
    errors = errors_for({"metrics": [ratio_metric(ratio={
        "kind": "average_ratio",
        "numerator": {"aggregation": "sum", "field": "amount"},
        "denominator": {"aggregation": "avg", "field": "order_id"},
    })]})
    assert any(error["code"] == "invalid_ratio_operand"
               and error["field"] == "ratio.numerator.aggregation"
               for error in errors)


def test_max_min_ratio_operands_are_rejected():
    errors = errors_for({"metrics": [ratio_metric(ratio={
        "numerator": {"aggregation": "max", "field": "amount"},
        "denominator": {"aggregation": "count", "field": "order_id"},
    })]})
    assert any(error["code"] == "unsupported_ratio_aggregation" for error in errors)


def test_ratio_without_ratio_block_is_rejected():
    errors = errors_for({"metrics": [valid_metric(aggregation="ratio")]})
    assert any(error["code"] == "missing_field" and error["field"] == "ratio"
               for error in errors)


def test_ratio_present_with_legacy_aggregation_is_still_a_ratio():
    metric = valid_metric(aggregation="avg", ratio={
        "kind": "average_ratio",
        "numerator": {"aggregation": "avg", "field": "amount"},
        "denominator": {"aggregation": "avg", "field": "order_id"},
    })
    loaded = dictionary_from_document({"metrics": [metric]}).metrics[0]
    assert loaded.aggregation == "ratio"
    assert loaded.ratio.kind == "average_ratio"


def test_ratio_semantics_change_the_fingerprint():
    first = dictionary_from_document({"metrics": [ratio_metric()]})
    second = dictionary_from_document({"metrics": [ratio_metric(ratio={
        "kind": "population_ratio",
        "numerator": {"aggregation": "sum", "field": "amount"},
        "denominator": {"aggregation": "count", "field": "order_id"},
        "zero_division": "coalesce_zero",
    })]})
    assert first.fingerprint != second.fingerprint
