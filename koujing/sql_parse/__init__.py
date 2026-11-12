"""Public SQL parsing facade with an automatic fallback path."""

from __future__ import annotations

import re

from .model import Aggregate, ParsedQuery
from .parser_fallback import parse_fallback
from .parser_primary import PrimaryParserError, PrimaryParserUnavailable, parse_primary

__all__ = [
    "Aggregate",
    "ParsedQuery",
    "PrimaryParserError",
    "PrimaryParserUnavailable",
    "parse_fallback",
    "parse_primary",
    "parse_sql",
]


def _clean_reason(reason: object) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(reason))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def parse_sql(sql: str, *, mode: str = "auto", dialect: str | None = None) -> ParsedQuery:
    """Parse SQL with sqlglot when possible, falling back without crashing.

    ``mode='primary'`` intentionally surfaces primary-parser failures.
    ``mode='auto'`` is the production path. ``mode='fallback'`` is useful for
    reproducibility checks and operation on minimal installations.
    """
    if mode not in {"auto", "fallback", "primary"}:
        raise ValueError(f"unknown parser mode: {mode}")
    if mode == "fallback":
        return parse_fallback(sql)
    try:
        return parse_primary(sql, dialect=dialect)
    except PrimaryParserUnavailable as exc:
        if mode == "primary":
            raise
        return parse_fallback(sql, extra_warnings=(
            f"primary parser unavailable: {_clean_reason(exc)}",
        ))
    except PrimaryParserError as exc:
        if mode == "primary":
            raise
        return parse_fallback(sql, extra_warnings=(
            f"primary parser failed; using fallback: {_clean_reason(exc)}",
        ))
    except Exception as exc:  # defensive: a parser bug must not abort a batch
        if mode == "primary":
            raise
        return parse_fallback(sql, extra_warnings=(
            f"primary parser crashed; using fallback: {_clean_reason(exc)}",
        ))
