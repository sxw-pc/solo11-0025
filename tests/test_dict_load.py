from __future__ import annotations

import json
import sys

import pytest

from conftest import DICT_MIN, DICT_YAML, valid_metric, write_dict
from koujing.dict_load import (
    DictValidationError,
    dictionary_from_document,
    load_dictionary,
)


def errors_for(document):
    with pytest.raises(DictValidationError) as caught:
        dictionary_from_document(document)
    return caught.value.errors


def test_fixture_normalisation_and_stable_fingerprint(dictionary):
    assert [metric.metric for metric in dictionary.metrics] == [
        "active_user", "order_gmv", "paid_order_count"
    ]
    order = dictionary.by_name()["order_gmv"]
    assert order.source_fields == ("order_paid_at", "order_status", "pay_amount")
    assert order.source_tables == ("fact_order",)
    assert len(dictionary.fingerprint) == 64
    again = load_dictionary(DICT_YAML, yaml_backend="lite")
    assert again.fingerprint == dictionary.fingerprint
    assert again.metrics == dictionary.metrics


def test_missing_required_field_reports_structured_error():
    metric = valid_metric()
    del metric["owner"]
    errors = errors_for({"metrics": [metric]})
    assert any(error["code"] == "missing_field" and error["field"] == "owner"
               for error in errors)


def test_duplicate_metric_is_rejected():
    errors = errors_for({"metrics": [valid_metric(), valid_metric()]})
    assert [error["code"] for error in errors] == ["duplicate_metric"]


def test_invalid_aggregation_is_rejected():
    errors = errors_for({"metrics": [valid_metric(aggregation="median")]})
    assert any(error["code"] == "invalid_aggregation" for error in errors)


def test_empty_source_fields_is_rejected():
    errors = errors_for({"metrics": [valid_metric(source_fields=[])]})
    assert any(error["code"] == "empty_source_fields" for error in errors)


@pytest.mark.parametrize("document, code", [
    ({}, "missing_metrics"),
    ({"metrics": {}}, "invalid_metrics"),
    ({"metrics": ["not-a-map"]}, "invalid_metric"),
    ({"metrics": [valid_metric(source_tables=[])]}, "empty_source_tables"),
])
def test_structural_errors(document, code):
    assert any(error["code"] == code for error in errors_for(document))


def test_filter_expr_is_optional_and_whitespace_is_normalised():
    metric = valid_metric(filter_expr="  status   =   'ok'  ")
    metric.pop("description")
    metric["description"] = "kept"
    dictionary = dictionary_from_document({"metrics": [metric]})
    assert dictionary.metrics[0].filter_expr == "status = 'ok'"


def test_fingerprint_changes_when_semantics_change():
    first = dictionary_from_document({"metrics": [valid_metric()]})
    second = dictionary_from_document({"metrics": [valid_metric(version="1.0.1")]})
    assert first.fingerprint != second.fingerprint


def test_yaml_lite_used_when_pyyaml_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    loaded = load_dictionary(DICT_MIN)
    assert loaded.yaml_backend == "lite"
    assert loaded.metrics[0].metric == "active_user"


def test_yaml_lite_matches_pyyaml_when_both_available():
    pytest.importorskip("yaml")
    lite = load_dictionary(DICT_YAML, yaml_backend="lite")
    pyyaml = load_dictionary(DICT_YAML, yaml_backend="pyyaml")
    assert lite.metrics == pyyaml.metrics
    assert lite.fingerprint == pyyaml.fingerprint


def test_json_dictionary_and_malformed_yaml(tmp_path):
    document = {"metrics": [valid_metric()]}
    json_path = write_dict(tmp_path, json.dumps(document), ".json")
    loaded = load_dictionary(json_path)
    assert loaded.yaml_backend == "json"
    with pytest.raises(DictValidationError) as caught:
        load_dictionary(write_dict(
            tmp_path, "metrics:\n  - metric: broken\n    unexpected continuation\n"
        ))
    assert caught.value.errors[0]["code"] == "yaml_parse_error"


def test_read_error_is_structured(tmp_path):
    with pytest.raises(DictValidationError) as caught:
        load_dictionary(tmp_path / "absent.yaml")
    assert caught.value.errors[0]["code"] == "read_error"
