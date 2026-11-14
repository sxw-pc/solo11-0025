"""Load, normalise and validate the declarative metric dictionary."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import yaml_lite
from .config import AGGREGATIONS, AGGREGATION_ALIASES, METRIC_REQUIRED, canonical_json, sha256_text
from .sql_parse.model import canonical_expr, sort_unique


class DictValidationError(ValueError):
    """A dictionary could not be read or failed structural validation."""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(f"{len(errors)} dictionary error(s)")
        self.errors = errors


@dataclass(frozen=True)
class JoinKeyPair:
    """One declared equality pairing: ``left_table.left_column`` to ``right_table.right_column``."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_table": self.left_table,
            "left_column": self.left_column,
            "right_table": self.right_table,
            "right_column": self.right_column,
        }


@dataclass(frozen=True)
class JoinContract:
    """Optional, explicit relationship declaration for one bounded two-table join.

    The contract is a *declaration*: it states which equality pairs and which
    candidate unique key on the right table the metric author relies on. It
    never proves the right table is actually unique at runtime.
    """

    join_type: str
    left_table: str
    right_table: str
    on: tuple[JoinKeyPair, ...]
    right_unique_key: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "join_type": self.join_type,
            "left_table": self.left_table,
            "right_table": self.right_table,
            "on": [pair.to_dict() for pair in self.on],
            "right_unique_key": list(self.right_unique_key),
        }


@dataclass(frozen=True)
class Metric:
    metric: str
    owner: str
    description: str
    source_tables: tuple[str, ...]
    source_fields: tuple[str, ...]
    aggregation: str
    filter_expr: str
    granularity: str
    version: str
    join_contract: JoinContract | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "metric": self.metric,
            "owner": self.owner,
            "description": self.description,
            "source_tables": list(self.source_tables),
            "source_fields": list(self.source_fields),
            "aggregation": self.aggregation,
            "filter_expr": self.filter_expr,
            "granularity": self.granularity,
            "version": self.version,
        }
        if self.join_contract is not None:
            data["join_contract"] = self.join_contract.to_dict()
        return data


@dataclass(frozen=True)
class Dictionary:
    metrics: tuple[Metric, ...]
    fingerprint: str
    source_path: str
    raw_text: str
    yaml_backend: str = "json"

    def by_name(self) -> dict[str, Metric]:
        return {item.metric: item for item in self.metrics}

    def metric_names(self) -> tuple[str, ...]:
        return tuple(item.metric for item in self.metrics)


def _error(code: str, message: str, *, index: int | None = None,
           metric: str = "", field: str = "") -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "index": index,
        "metric": metric,
        "field": field,
    }


def _load_yaml(text: str, backend: str) -> tuple[Any, str]:
    if backend not in {"auto", "pyyaml", "lite"}:
        raise DictValidationError([_error("invalid_yaml_backend", f"unknown YAML backend: {backend}")])
    if backend in {"auto", "pyyaml"}:
        try:
            yaml = importlib.import_module("yaml")
            loaded = yaml.safe_load(text)
            return loaded, "pyyaml"
        except (ImportError, ModuleNotFoundError):
            if backend == "pyyaml":
                raise DictValidationError(
                    [_error("pyyaml_unavailable", "PyYAML is not available")]
                ) from None
        except Exception as exc:  # PyYAML exposes several parser exception types
            raise DictValidationError(
                [_error("yaml_parse_error", f"PyYAML could not parse dictionary: {exc}")]
            ) from None
    try:
        return yaml_lite.loads(text), "lite"
    except yaml_lite.YamlLiteError as exc:
        raise DictValidationError(
            [_error("yaml_parse_error", f"fallback YAML parser failed: {exc}")]
        ) from None


def _as_list(value: Any, *, field: str, index: int, metric: str,
             errors: list[dict[str, Any]]) -> tuple[str, ...]:
    if value is None:
        errors.append(_error("missing_field", f"{field} is required", index=index,
                             metric=metric, field=field))
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        errors.append(_error("invalid_field_type", f"{field} must be a list", index=index,
                             metric=metric, field=field))
        return ()
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, (str, int, float)):
            errors.append(_error("invalid_list_item", f"{field} contains a non-string item",
                                 index=index, metric=metric, field=field))
            return ()
        text = str(item).strip().lower()
        if text:
            cleaned.append(text)
    return sort_unique(cleaned)


def _normalise_name(value: Any) -> str:
    return str(value).strip().strip("`\"[]").lower()


def _validate_contract_pairs(raw_pairs: Any, *, index: int, metric: str,
                             errors: list[dict[str, Any]]) -> None:
    """Structurally validate the ``on`` pairing list, appending any errors."""
    if not isinstance(raw_pairs, (list, tuple)):
        errors.append(_error("invalid_join_contract",
                             "join_contract.on must be a list of column pairings",
                             index=index, metric=metric, field="join_contract.on"))
        return
    for position, pair in enumerate(raw_pairs):
        if not isinstance(pair, dict):
            errors.append(_error("invalid_join_contract",
                                 f"join_contract.on[{position}] must be a mapping",
                                 index=index, metric=metric, field="join_contract.on"))
            continue
        left_col = _normalise_name(pair.get("left_column", ""))
        right_col = _normalise_name(pair.get("right_column", ""))
        if not left_col or not right_col:
            errors.append(_error("invalid_join_contract",
                                 f"join_contract.on[{position}] requires left_column "
                                 "and right_column",
                                 index=index, metric=metric, field="join_contract.on"))


def _normalise_join_contract(raw: Any, *, index: int, metric: str,
                             errors: list[dict[str, Any]]) -> JoinContract | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        errors.append(_error("invalid_join_contract",
                             "join_contract must be a mapping",
                             index=index, metric=metric, field="join_contract"))
        return None
    join_type = _normalise_name(raw.get("join_type", ""))
    if join_type not in {"inner", "left"}:
        errors.append(_error("invalid_join_contract",
                             "join_contract.join_type must be 'inner' or 'left'",
                             index=index, metric=metric,
                             field="join_contract.join_type"))
    left_table = _normalise_name(raw.get("left_table", ""))
    right_table = _normalise_name(raw.get("right_table", ""))
    for column_name, value in (("left_table", left_table), ("right_table", right_table)):
        if not value:
            errors.append(_error("invalid_join_contract",
                                 f"join_contract.{column_name} is required",
                                 index=index, metric=metric,
                                 field=f"join_contract.{column_name}"))
    if left_table and right_table and left_table == right_table:
        errors.append(_error("invalid_join_contract",
                             "join_contract.left_table and right_table must differ",
                             index=index, metric=metric, field="join_contract"))

    # YAML 1.1 parsers (notably PyYAML) read the bare key ``on`` as the
    # boolean True, so accept both spellings transparently.
    raw_pairs = raw.get("on")
    if raw_pairs is None:
        raw_pairs = raw.get(True)
    _validate_contract_pairs(raw_pairs, index=index, metric=metric, errors=errors)
    pairs: list[JoinKeyPair] = []
    if isinstance(raw_pairs, (list, tuple)):
        for pair in raw_pairs:
            if not isinstance(pair, dict):
                continue
            left_col = _normalise_name(pair.get("left_column", ""))
            right_col = _normalise_name(pair.get("right_column", ""))
            if not left_col or not right_col:
                continue
            pair_left_table = _normalise_name(pair.get("left_table", left_table)) or left_table
            pair_right_table = _normalise_name(pair.get("right_table", right_table)) or right_table
            if pair_left_table != left_table or pair_right_table != right_table:
                errors.append(_error("invalid_join_contract",
                                     "join_contract.on pair references tables other than "
                                     "the declared left_table/right_table",
                                     index=index, metric=metric,
                                     field="join_contract.on"))
            pairs.append(JoinKeyPair(
                left_table=pair_left_table,
                left_column=left_col,
                right_table=pair_right_table,
                right_column=right_col,
            ))
    if not pairs:
        errors.append(_error("invalid_join_contract",
                             "join_contract.on must declare at least one equality pair",
                             index=index, metric=metric, field="join_contract.on"))
    seen_pairs: set[tuple[str, str]] = set()
    for pair in pairs:
        signature = (pair.left_column, pair.right_column)
        if signature in seen_pairs:
            errors.append(_error("invalid_join_contract",
                                 "duplicate equality pair in join_contract.on",
                                 index=index, metric=metric, field="join_contract.on"))
        seen_pairs.add(signature)

    unique_key = ()
    raw_key = raw.get("right_unique_key")
    if raw_key in (None, ""):
        errors.append(_error("invalid_join_contract",
                             "join_contract.right_unique_key is required",
                             index=index, metric=metric,
                             field="join_contract.right_unique_key"))
    else:
        unique_key = _as_list(raw_key, field="join_contract.right_unique_key",
                              index=index, metric=metric, errors=errors)
        if isinstance(raw_key, (list, tuple)) and not unique_key:
            errors.append(_error("invalid_join_contract",
                                 "join_contract.right_unique_key must not be empty",
                                 index=index, metric=metric,
                                 field="join_contract.right_unique_key"))
        if unique_key:
            key_set = set(unique_key)
            paired_right = {pair.right_column for pair in pairs}
            uncovered = sorted(key_set - paired_right)
            if uncovered:
                errors.append(_error("invalid_join_contract",
                                     "right_unique_key columns missing from join_contract.on "
                                     f"right side: {', '.join(uncovered)}",
                                     index=index, metric=metric,
                                     field="join_contract.right_unique_key"))
    if errors:
        return None
    ordered_pairs = tuple(sorted(pairs, key=lambda item: (item.left_column, item.right_column)))
    return JoinContract(
        join_type=join_type,
        left_table=left_table,
        right_table=right_table,
        on=ordered_pairs,
        right_unique_key=unique_key,
    )


def _normalise_metric(raw: Any, index: int, errors: list[dict[str, Any]]) -> Metric | None:
    if not isinstance(raw, dict):
        errors.append(_error("invalid_metric", "each metric must be a mapping", index=index))
        return None
    name_value = raw.get("metric")
    name = str(name_value).strip().lower() if name_value is not None else ""
    if not name:
        errors.append(_error("missing_field", "metric is required", index=index,
                             field="metric"))
        name = f"<entry {index}>"
    for field_name in METRIC_REQUIRED:
        if field_name not in raw or raw.get(field_name) in (None, ""):
            errors.append(_error("missing_field", f"{field_name} is required", index=index,
                                 metric=name, field=field_name))

    tables = ()
    if raw.get("source_tables") not in (None, ""):
        tables = _as_list(raw.get("source_tables"), field="source_tables", index=index,
                          metric=name, errors=errors)
    fields = ()
    if raw.get("source_fields") not in (None, ""):
        fields = _as_list(raw.get("source_fields"), field="source_fields", index=index,
                          metric=name, errors=errors)
    if isinstance(raw.get("source_fields"), (list, tuple)) and not fields:
        errors.append(_error("empty_source_fields", "source_fields must not be empty",
                             index=index, metric=name, field="source_fields"))
    if isinstance(raw.get("source_tables"), (list, tuple)) and not tables:
        errors.append(_error("empty_source_tables", "source_tables must not be empty",
                             index=index, metric=name, field="source_tables"))

    aggregation_raw = str(raw.get("aggregation", "")).strip().lower()
    aggregation = AGGREGATION_ALIASES.get(aggregation_raw, aggregation_raw)
    if aggregation_raw and aggregation not in AGGREGATIONS:
        errors.append(_error("invalid_aggregation",
                             f"unsupported aggregation: {aggregation_raw!r}",
                             index=index, metric=name, field="aggregation"))
    owner = str(raw.get("owner", "")).strip()
    description = str(raw.get("description", "")).strip()
    filter_expr = canonical_expr(raw.get("filter_expr"))
    granularity = str(raw.get("granularity", "")).strip().lower()
    version = str(raw.get("version", "")).strip()
    join_contract = _normalise_join_contract(
        raw.get("join_contract"), index=index, metric=name, errors=errors
    )
    if join_contract is not None and tables:
        contract_tables = {join_contract.left_table, join_contract.right_table}
        unknown = sorted(contract_tables - set(tables))
        if unknown:
            errors.append(_error("invalid_join_contract",
                                 "join_contract tables not declared in source_tables: "
                                 f"{', '.join(unknown)}",
                                 index=index, metric=name, field="join_contract"))
            join_contract = None
    return Metric(
        metric=name,
        owner=owner,
        description=description,
        source_tables=tables,
        source_fields=fields,
        aggregation=aggregation,
        filter_expr=filter_expr,
        granularity=granularity,
        version=version,
        join_contract=join_contract,
    )


def dictionary_from_document(document: Any, *, source_path: str = "<memory>",
                             raw_text: str = "", yaml_backend: str = "json") -> Dictionary:
    """Validate an already-decoded document and produce a fingerprint."""
    errors: list[dict[str, Any]] = []
    if isinstance(document, dict):
        entries = document.get("metrics")
        if entries is None:
            errors.append(_error("missing_metrics", "top-level 'metrics' list is required"))
            entries = []
    elif isinstance(document, list):
        entries = document
    else:
        errors.append(_error("invalid_root", "dictionary root must be a mapping or list"))
        entries = []
    if not isinstance(entries, list):
        errors.append(_error("invalid_metrics", "'metrics' must be a list"))
        entries = []

    metrics: list[Metric] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        item = _normalise_metric(raw, index, errors)
        if item is None:
            continue
        if item.metric in seen:
            errors.append(_error("duplicate_metric",
                                 f"duplicate metric: {item.metric}",
                                 index=index, metric=item.metric, field="metric"))
        else:
            seen.add(item.metric)
            metrics.append(item)
    if not metrics and not errors:
        errors.append(_error("empty_dictionary", "dictionary contains no metrics"))
    if errors:
        raise DictValidationError(errors)
    metrics.sort(key=lambda item: item.metric)
    payload = [item.to_dict() for item in metrics]
    fingerprint = sha256_text(canonical_json(payload))
    return Dictionary(
        metrics=tuple(metrics),
        fingerprint=fingerprint,
        source_path=source_path,
        raw_text=raw_text,
        yaml_backend=yaml_backend,
    )


def load_dictionary(path: str | Path, *, yaml_backend: str = "auto") -> Dictionary:
    """Load a YAML or JSON dictionary from disk."""
    path_obj = Path(path)
    try:
        text = path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        raise DictValidationError(
            [_error("read_error", f"cannot read dictionary: {exc}")]
        ) from None
    if path_obj.suffix.lower() == ".json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DictValidationError(
                [_error("json_parse_error", f"JSON parser failed: {exc}")]
            ) from None
        backend = "json"
    else:
        document, backend = _load_yaml(text, yaml_backend)
        if document is None:
            document = {}
    return dictionary_from_document(
        document,
        source_path=str(path_obj),
        raw_text=text,
        yaml_backend=backend,
    )


def normalised_copy(dictionary: Dictionary, **changes: Any) -> Dictionary:
    """Small convenience used by tests and future dictionary overlays."""
    return replace(dictionary, **changes)
