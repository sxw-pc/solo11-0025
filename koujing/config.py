"""Shared paths, constants and small helpers for koujing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"

DEFAULT_DICT = PROJECT_ROOT / "fixtures" / "dict" / "metrics.yaml"
DEFAULT_REPORTS = PROJECT_ROOT / "fixtures" / "reports"
DEFAULT_DB = PROJECT_ROOT / "data" / "koujing.db"
DEFAULT_OUT = PROJECT_ROOT / "out"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SERVER_NAME = "koujing/1.0"

CATEGORIES = ("consistent", "drift", "missing_field", "unregistered")
CATEGORY_LABELS = {
    "consistent": "一致",
    "drift": "偏差",
    "missing_field": "缺失字段",
    "unregistered": "未登记",
}
CATEGORY_ORDER = {name: index for index, name in enumerate(CATEGORIES)}
FAILABLE_CATEGORIES = ("drift", "missing_field", "unregistered")

AGGREGATIONS = ("sum", "count", "count_distinct", "avg", "max", "min")
AGGREGATION_ALIASES = {
    "count(distinct)": "count_distinct",
    "count_distinct": "count_distinct",
    "distinct_count": "count_distinct",
}

METRIC_REQUIRED = (
    "metric",
    "owner",
    "description",
    "source_tables",
    "source_fields",
    "aggregation",
    "granularity",
    "version",
)

SQL_KEYWORDS = {
    "all", "and", "any", "as", "asc", "between", "by", "case", "cast",
    "coalesce", "current_date", "current_timestamp", "date", "day", "desc",
    "distinct", "else", "end", "false", "from", "group", "having", "in",
    "inner", "interval", "is", "join", "left", "like", "limit", "month",
    "not", "null", "on", "or", "order", "outer", "over", "partition",
    "qualify", "quarter", "right", "row", "rows", "select", "then", "true",
    "union", "week", "when", "where", "window", "year",
}

PARSER_MODES = ("auto", "fallback", "primary")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def relative_display(path: str | Path, root: str | Path | None = None) -> str:
    """Return a stable project-relative POSIX path when possible."""
    path_obj = Path(path)
    base = Path(root) if root is not None else PROJECT_ROOT
    try:
        return path_obj.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        return path_obj.name


def ensure_within(path: str | Path, base: str | Path) -> Path:
    """Resolve *path* and assert it stays below *base*."""
    base_path = Path(base).resolve()
    target = Path(path).resolve()
    if target != base_path and base_path not in target.parents:
        raise ValueError("path escapes the allowed directory")
    return target


def safe_batch_component(batch_id: str) -> str:
    """Validate a batch id before it is used as a filesystem component."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")
    if not batch_id or len(batch_id) > 96 or any(ch not in allowed for ch in batch_id):
        raise ValueError("invalid batch id")
    if batch_id in {".", ".."}:
        raise ValueError("invalid batch id")
    return batch_id
