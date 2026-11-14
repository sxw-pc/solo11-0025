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
    # Optional business-day start as minutes after UTC midnight. Only allowed
    # for day-granularity metrics; ``None`` keeps the legacy granularity-only rule.
    business_day_start_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        # Evidence always carries the expected origin (``None`` when undeclared)
        # so reports and ledgers retain both expected and actual day origins.
        return {
            "metric": self.metric,
            "owner": self.owner,
            "description": self.description,
            "source_tables": list(self.source_tables),
            "source_fields": list(self.source_fields),
            "aggregation": self.aggregation,
            "filter_expr": self.filter_expr,
            "granularity": self.granularity,
            "version": self.version,
            "business_day_start_minutes": self.business_day_start_minutes,
        }

    def fingerprint_payload(self) -> dict[str, Any]:
        """Normalised mapping used for the dictionary fingerprint.

        The optional origin is omitted when undeclared so legacy dictionaries
        keep their original fingerprint.
        """
        data = self.to_dict()
        if data["business_day_start_minutes"] is None:
            del data["business_day_start_minutes"]
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
    business_day_start_minutes = _normalise_day_start(
        raw.get("business_day_start_minutes"), granularity, name, index, errors
    )
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
        business_day_start_minutes=business_day_start_minutes,
    )


def _normalise_day_start(raw: Any, granularity: str, metric: str, index: int,
                         errors: list[dict[str, Any]]) -> int | None:
    """Validate the optional business-day origin, expressed in UTC minutes."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        errors.append(_error(
            "invalid_business_day_start",
            "business_day_start_minutes must be an integer number of minutes",
            index=index, metric=metric, field="business_day_start_minutes",
        ))
        return None
    if not 0 <= raw < 24 * 60:
        errors.append(_error(
            "invalid_business_day_start",
            "business_day_start_minutes must be within 0..1439 minutes",
            index=index, metric=metric, field="business_day_start_minutes",
        ))
        return None
    if granularity and granularity != "day":
        errors.append(_error(
            "invalid_business_day_start",
            "business_day_start_minutes is only allowed for day-granularity metrics",
            index=index, metric=metric, field="business_day_start_minutes",
        ))
        return None
    return raw


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
    payload = [item.fingerprint_payload() for item in metrics]
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
