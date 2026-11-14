"""Load, normalise and validate the declarative metric dictionary."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import yaml_lite
from .config import (
    AGGREGATIONS,
    AGGREGATION_ALIASES,
    METRIC_REQUIRED,
    RATIO_AGGREGATION,
    RATIO_AGGREGATION_ALIASES,
    RATIO_KIND_ALIASES,
    RATIO_KINDS,
    ZERO_DIVISION_ALIASES,
    ZERO_DIVISION_POLICIES,
    canonical_json,
    sha256_text,
)
from .sql_parse.model import canonical_expr, sort_unique


class DictValidationError(ValueError):
    """A dictionary could not be read or failed structural validation."""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(f"{len(errors)} dictionary error(s)")
        self.errors = errors


@dataclass(frozen=True)
class RatioOperand:
    """Declared numerator or denominator of a derived ratio metric."""

    aggregation: str
    field: str

    @property
    def expression(self) -> str:
        return canonical_expr(self.field)

    def to_dict(self) -> dict[str, Any]:
        return {"aggregation": self.aggregation, "field": self.field}


@dataclass(frozen=True)
class MetricRatio:
    """Declared ratio contract: numerator, denominator and zero policy."""

    kind: str
    numerator: RatioOperand
    denominator: RatioOperand
    zero_division: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "numerator": self.numerator.to_dict(),
            "denominator": self.denominator.to_dict(),
            "zero_division": self.zero_division,
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
    ratio: MetricRatio | None = None

    @property
    def is_ratio(self) -> bool:
        return self.ratio is not None

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
        # Keep the legacy payload byte-identical for single-aggregate metrics so
        # their dictionary fingerprint does not change when ratio support lands.
        if self.ratio is not None:
            data["ratio"] = self.ratio.to_dict()
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


def _normalise_ratio_operand(value: Any, *, side: str, index: int, metric: str,
                             errors: list[dict[str, Any]]) -> RatioOperand | None:
    """Validate one declared ratio operand: ``{aggregation, field}``."""
    field_name = f"ratio.{side}"
    if not isinstance(value, dict):
        errors.append(_error("invalid_ratio_operand",
                             f"ratio.{side} must be a mapping with aggregation and field",
                             index=index, metric=metric, field=field_name))
        return None
    aggregation_raw = str(value.get("aggregation", "")).strip().lower()
    aggregation = AGGREGATION_ALIASES.get(aggregation_raw, aggregation_raw)
    if not aggregation_raw:
        errors.append(_error("missing_field", f"ratio.{side}.aggregation is required",
                             index=index, metric=metric, field=f"{field_name}.aggregation"))
    elif aggregation not in AGGREGATIONS:
        errors.append(_error("invalid_aggregation",
                             f"unsupported aggregation: {aggregation_raw!r}",
                             index=index, metric=metric, field=f"{field_name}.aggregation"))
    field_raw = value.get("field")
    operand_field = str(field_raw).strip() if field_raw is not None else ""
    if not operand_field:
        errors.append(_error("missing_field", f"ratio.{side}.field is required",
                             index=index, metric=metric, field=f"{field_name}.field"))
    return RatioOperand(aggregation=aggregation, field=canonical_expr(operand_field))


def _normalise_ratio(value: Any, *, index: int, metric: str, aggregation: str,
                     errors: list[dict[str, Any]]) -> MetricRatio | None:
    if not isinstance(value, dict):
        errors.append(_error("invalid_ratio", "ratio must be a mapping",
                             index=index, metric=metric, field="ratio"))
        return None

    kind_raw = str(value.get("kind", "")).strip().lower()
    if kind_raw:
        kind = RATIO_KIND_ALIASES.get(kind_raw, kind_raw)
        if kind not in RATIO_KINDS:
            errors.append(_error("invalid_ratio_kind",
                                 f"unsupported ratio kind: {kind_raw!r}",
                                 index=index, metric=metric, field="ratio.kind"))
    else:
        legacy = AGGREGATION_ALIASES.get(aggregation, aggregation)
        kind = "average_ratio" if legacy == "avg" else "population_ratio"

    zero_raw = str(value.get("zero_division", "")).strip().lower()
    zero_division = "nullif"
    if zero_raw:
        zero_division = ZERO_DIVISION_ALIASES.get(zero_raw, zero_raw)
        if zero_division not in ZERO_DIVISION_POLICIES:
            errors.append(_error("invalid_zero_division",
                                 f"unsupported zero_division policy: {zero_raw!r}",
                                 index=index, metric=metric, field="ratio.zero_division"))

    numerator = _normalise_ratio_operand(value.get("numerator"), side="numerator",
                                         index=index, metric=metric, errors=errors)
    denominator = _normalise_ratio_operand(value.get("denominator"), side="denominator",
                                           index=index, metric=metric, errors=errors)
    if numerator is None or denominator is None:
        return None

    if kind == "average_ratio":
        # Per-row ratio: the two operands are fields, not aggregates.
        for side, operand in (("numerator", numerator), ("denominator", denominator)):
            if operand.aggregation != "avg":
                errors.append(_error(
                    "invalid_ratio_operand",
                    f"average_ratio expects ratio.{side}.aggregation='avg' (row-level operand)",
                    index=index, metric=metric, field=f"ratio.{side}.aggregation",
                ))
    for side, operand in (("numerator", numerator), ("denominator", denominator)):
        if operand.aggregation in {"max", "min"}:
            errors.append(_error(
                "unsupported_ratio_aggregation",
                f"ratio.{side}.aggregation={operand.aggregation!r} is not supported "
                "for ratio operands",
                index=index, metric=metric, field=f"ratio.{side}.aggregation",
            ))
    return MetricRatio(kind=kind, numerator=numerator, denominator=denominator,
                       zero_division=zero_division)


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
    ratio_raw = raw.get("ratio")
    ratio: MetricRatio | None = None
    declared_ratio = ratio_raw not in (None, "") or aggregation_raw in RATIO_AGGREGATION_ALIASES
    if aggregation_raw in RATIO_AGGREGATION_ALIASES:
        aggregation = RATIO_AGGREGATION
    elif aggregation_raw and aggregation not in AGGREGATIONS:
        errors.append(_error("invalid_aggregation",
                             f"unsupported aggregation: {aggregation_raw!r}",
                             index=index, metric=name, field="aggregation"))
    if declared_ratio and ratio_raw in (None, ""):
        errors.append(_error("missing_field", "ratio is required when aggregation='ratio'",
                             index=index, metric=name, field="ratio"))
    elif declared_ratio:
        ratio = _normalise_ratio(ratio_raw, index=index, metric=name,
                                 aggregation=aggregation, errors=errors)
    if ratio is not None:
        aggregation = RATIO_AGGREGATION

    owner = str(raw.get("owner", "")).strip()
    description = str(raw.get("description", "")).strip()
    filter_expr = canonical_expr(raw.get("filter_expr"))
    granularity = str(raw.get("granularity", "")).strip().lower()
    version = str(raw.get("version", "")).strip()
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
        ratio=ratio,
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
