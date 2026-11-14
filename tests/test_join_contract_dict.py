from __future__ import annotations

import pytest

from conftest import valid_metric, write_dict
from koujing.dict_load import (
    DictValidationError,
    JoinContract,
    dictionary_from_document,
    load_dictionary,
)


def valid_contract(**overrides):
    contract = {
        "join_type": "left",
        "left_table": "fact_order",
        "right_table": "dim_shop",
        "on": [
            {"left_column": "shop_id", "right_column": "shop_id"},
            {"left_column": "city_id", "right_column": "city_id"},
        ],
        "right_unique_key": ["shop_id", "city_id"],
    }
    contract.update(overrides)
    return contract


def metric_with_contract(contract=None):
    metric = valid_metric(
        metric="shop_gmv",
        source_tables=["fact_order", "dim_shop"],
        source_fields=["pay_amount", "shop_id", "city_id"],
    )
    if contract is not None:
        metric["join_contract"] = contract
    return metric


def errors_for(document):
    with pytest.raises(DictValidationError) as caught:
        dictionary_from_document(document)
    return caught.value.errors


def test_join_contract_is_optional_and_legacy_metrics_keep_none():
    dictionary = dictionary_from_document({"metrics": [valid_metric()]})
    assert dictionary.metrics[0].join_contract is None
    assert "join_contract" not in dictionary.metrics[0].to_dict()


def test_valid_join_contract_is_normalised_and_sorted():
    dictionary = dictionary_from_document(
        {"metrics": [metric_with_contract(valid_contract())]}
    )
    contract = dictionary.metrics[0].join_contract
    assert isinstance(contract, JoinContract)
    assert contract.join_type == "left"
    assert (contract.left_table, contract.right_table) == ("fact_order", "dim_shop")
    # Equality pairs are stored deterministically regardless of input order.
    assert [pair.left_column for pair in contract.on] == ["city_id", "shop_id"]
    assert contract.right_unique_key == ("city_id", "shop_id")
    payload = dictionary.metrics[0].to_dict()["join_contract"]
    assert payload["on"][0]["left_table"] == "fact_order"
    assert payload["right_unique_key"] == ["city_id", "shop_id"]


def test_contract_changes_the_dictionary_fingerprint():
    plain = dictionary_from_document({"metrics": [metric_with_contract(None)]})
    with_contract = dictionary_from_document(
        {"metrics": [metric_with_contract(valid_contract())]}
    )
    assert plain.fingerprint != with_contract.fingerprint


@pytest.mark.parametrize("field, override", [
    ("join_type", {"join_type": "right"}),
    ("left_table", {"left_table": ""}),
    ("right_table", {"right_table": ""}),
    ("right_unique_key", {"right_unique_key": []}),
])
def test_invalid_contract_fields_are_rejected(field, override):
    errors = errors_for({"metrics": [metric_with_contract(valid_contract(**override))]})
    assert any(error["code"] == "invalid_join_contract" and field in error["field"]
               for error in errors)


def test_contract_rejects_same_table_on_both_sides():
    contract = valid_contract(right_table="fact_order")
    errors = errors_for({"metrics": [metric_with_contract(contract)]})
    assert any(error["code"] == "invalid_join_contract" for error in errors)


def test_contract_rejects_tables_outside_source_tables():
    contract = valid_contract(right_table="dim_region")
    errors = errors_for({"metrics": [metric_with_contract(contract)]})
    assert any(error["code"] == "invalid_join_contract"
               and "source_tables" in error["message"] for error in errors)


def test_unique_key_must_be_covered_by_on_pairs():
    contract = valid_contract(
        right_unique_key=["shop_id", "city_id", "region_id"],
    )
    errors = errors_for({"metrics": [metric_with_contract(contract)]})
    assert any("right_unique_key" in error["field"] and "region_id" in error["message"]
               for error in errors)


def test_duplicate_equality_pairs_are_rejected():
    contract = valid_contract(on=[
        {"left_column": "shop_id", "right_column": "shop_id"},
        {"left_column": "shop_id", "right_column": "shop_id"},
    ], right_unique_key=["shop_id"])
    errors = errors_for({"metrics": [metric_with_contract(contract)]})
    assert any("duplicate equality pair" in error["message"] for error in errors)


def test_empty_on_list_is_rejected():
    errors = errors_for({"metrics": [metric_with_contract(valid_contract(on=[]))]})
    assert any("at least one equality pair" in error["message"] for error in errors)


def test_pair_table_overrides_must_match_contract_roles():
    contract = valid_contract(on=[
        {"left_table": "dim_shop", "left_column": "shop_id",
         "right_table": "fact_order", "right_column": "shop_id"},
        {"left_column": "city_id", "right_column": "city_id"},
    ])
    errors = errors_for({"metrics": [metric_with_contract(contract)]})
    assert any("references tables other than" in error["message"] for error in errors)


def test_join_contract_loads_from_yaml_and_json(tmp_path):
    metric = metric_with_contract(valid_contract())
    document = {"metrics": [metric]}
    import json

    json_path = write_dict(tmp_path, json.dumps(document), ".json")
    loaded_json = load_dictionary(json_path)
    assert loaded_json.metrics[0].join_contract.join_type == "left"

    yaml_text = """
metrics:
  - metric: shop_gmv
    owner: trade
    description: 门店成交金额
    source_tables: [fact_order, dim_shop]
    source_fields: [pay_amount, shop_id, city_id]
    aggregation: sum
    granularity: day
    version: 1.0.0
    join_contract:
      join_type: left
      left_table: fact_order
      right_table: dim_shop
      on:
        - left_column: shop_id
          right_column: shop_id
        - left_column: city_id
          right_column: city_id
      right_unique_key: [shop_id, city_id]
"""
    yaml_path = write_dict(tmp_path, yaml_text, ".yaml")
    loaded_yaml = load_dictionary(yaml_path, yaml_backend="lite")
    assert loaded_yaml.metrics[0].join_contract == loaded_json.metrics[0].join_contract


def test_bare_on_key_survives_yaml_boolean_coercion(tmp_path):
    # PyYAML follows YAML 1.1 and turns the bare key "on" into True; both
    # backends must still produce the same join contract.
    yaml_text = """
metrics:
  - metric: shop_gmv
    owner: trade
    description: 门店成交金额
    source_tables: [fact_order, dim_shop]
    source_fields: [pay_amount, shop_id, city_id]
    aggregation: sum
    granularity: day
    version: 1.0.0
    join_contract:
      join_type: inner
      left_table: fact_order
      right_table: dim_shop
      on:
        - left_column: shop_id
          right_column: shop_id
      right_unique_key: [shop_id]
"""
    yaml_path = write_dict(tmp_path, yaml_text, ".yaml")
    lite = load_dictionary(yaml_path, yaml_backend="lite")
    assert lite.metrics[0].join_contract.on[0].left_column == "shop_id"
    pytest.importorskip("yaml")
    pyyaml = load_dictionary(yaml_path, yaml_backend="pyyaml")
    assert pyyaml.metrics[0].join_contract == lite.metrics[0].join_contract
