"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: Sentinel for a ``day`` bucket whose business-day origin cannot be decided
#: statically (dynamic offset, non-constant expression, unsupported unit ...).
UNKNOWN_DAY_ORIGIN = -1


@dataclass(frozen=True)
class Aggregate:
    """One aggregate projection such as ``SUM(pay_amount) AS order_gmv``."""

    function: str
    expression: str
    alias: str
    fields: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "expression": self.expression,
            "alias": self.alias,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class ParsedQuery:
    """The stable subset of a SELECT statement this project understands."""

    source: str
    tables: tuple[str, ...]
    select_fields: tuple[str, ...]
    aggregates: tuple[Aggregate, ...]
    filter_expr: str
    group_by: tuple[str, ...]
    granularity: str
    # Minute offset (0..1439) of the business-day start for day buckets,
    # UNKNOWN_DAY_ORIGIN when it cannot be statically decided, or ``None``
    # for non-day granularities.
    day_origin_minutes: int | None
    warnings: tuple[str, ...]
    line_start: int
    line_end: int

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aggregates"] = [agg.to_dict() for agg in self.aggregates]
        return data

    def comparable(self) -> dict:
        """Fields that must agree between primary and fallback parsers."""
        return {
            "tables": list(self.tables),
            "select_fields": list(self.select_fields),
            "aggregates": [agg.to_dict() for agg in self.aggregates],
            "filter_expr": self.filter_expr,
            "group_by": list(self.group_by),
            "granularity": self.granularity,
            "day_origin_minutes": self.day_origin_minutes,
        }


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_STRING_RE = re.compile(r"'(?:''|\\.|[^'])*'")
_QUALIFIER_RE = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\s*\.\s*)+([A-Za-z_]\w*)")


def canonical_expr(text: str | None) -> str:
    """Canonical, conservative comparison form for filters and expressions.

    Case is folded outside single-quoted string literals, table qualifiers are
    removed, whitespace is collapsed and operator spacing is normalised. String
    literal contents are preserved verbatim. Boolean terms are not reordered:
    doing so would claim semantic equivalence we cannot statically prove.
    """
    if not text:
        return ""
    source = str(text).strip().rstrip(";").strip()
    literals: list[str] = []

    def protect(match: re.Match[str]) -> str:
        literals.append(match.group(0))
        return f"\x02{len(literals) - 1}\x02"

    protected = _STRING_RE.sub(protect, source)
    value = protected.lower()
    value = _QUALIFIER_RE.sub(r"\1", value)
    value = re.sub(r"\s*(<>|<=|>=|!=|=|<|>|\+|\-|\*|/)\s*", r" \1 ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\(\s+", "(", value)
    value = re.sub(r"\s+\)", ")", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace(" != ", " <> ")
    for index, literal in enumerate(literals):
        value = value.replace(f"\x02{index}\x02", literal)
    # Normalise the PostgreSQL-style ``column::date`` shorthand and sqlglot's
    # CAST rendering into one shared form so both parsers agree on GROUP BY.
    value = re.sub(
        r"([A-Za-z_][\w.]*)\s*::\s*date\b",
        r"cast(\1 as date)",
        value,
    )
    # sqlglot renders the DATE_TRUNC unit string in upper case while the
    # fallback parser preserves the source spelling; fold it so both parsers
    # produce identical canonical GROUP BY expressions.
    value = re.sub(
        r"(date_trunc\s*\(\s*)('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")",
        lambda match: match.group(1) + match.group(2).lower(),
        value,
    )
    return value


_DAY_CAST_RE = re.compile(r"cast\s*\(.*\bas\s+date\s*\)|::\s*date", re.I)


def detect_granularity(group_by: tuple[str, ...] | list[str]) -> str:
    """Map common GROUP BY expressions to a granularity label."""
    if not group_by:
        return "none"
    joined = " ".join(item.lower() for item in group_by)
    checks = (
        ("quarter", ("quarter",)),
        ("month", ("month",)),
        ("week", ("week",)),
        ("year", ("year",)),
        ("day", ("date(", "date (", "day(", "day (",
                 "date_trunc('day'", 'date_trunc("day"')),
    )
    for label, needles in checks:
        if any(needle in joined for needle in needles):
            return label
    if _DAY_CAST_RE.search(joined):
        return "day"
    return "row"


# A single-quoted literal inside a canonical expression (contents preserved).
_INTERVAL_RE = re.compile(
    r"\s*(?P<sign>[+-])\s*interval\s+'(?P<value>\d+)'\s+(?P<unit>hours?|minutes?)\b",
    re.I,
)
_DATE_TRUNC_DAY_RE = re.compile(r"^date_trunc\(\s*(['\"])(day)\1\s*,\s*(?P<inner>.*)\)$", re.I | re.S)
_DATE_PLAIN_RE = re.compile(r"^(?:date|day)\s*\(.*\)$", re.I | re.S)
_CAST_DATE_RE = re.compile(r"^cast\s*\(.*\bas\s+date\s*\)$", re.I | re.S)
_COLON_DATE_RE = re.compile(r"^[A-Za-z_][\w.]*\s*::\s*date$", re.I | re.S)
# A bare (possibly schema-qualified) column name, nothing else.
_BARE_COLUMN_RE = re.compile(r"^[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*$")
_DAY_MINUTES = 24 * 60


def _classify_day_item(item: str) -> int:
    """Classify one canonical day-bucket expression.

    Returns the business-day origin in minutes (0..1439), or
    :data:`UNKNOWN_DAY_ORIGIN` when the expression cannot be reliably
    interpreted. Only constant ``INTERVAL`` minute/hour offsets of a bare
    UTC timestamp column are accepted; any dynamic or non-constant
    expression is reported as unknown rather than silently treated as zero.
    """
    text = collapse_whitespace(item)
    if _DATE_PLAIN_RE.match(text) or _CAST_DATE_RE.match(text) or _COLON_DATE_RE.match(text):
        return 0
    match = _DATE_TRUNC_DAY_RE.match(text)
    if match is None:
        return UNKNOWN_DAY_ORIGIN
    inner = collapse_whitespace(match.group("inner"))
    intervals = list(_INTERVAL_RE.finditer(inner))
    if not intervals:
        # DATE_TRUNC('day', <column>) with no offset: UTC midnight origin.
        return 0 if _BARE_COLUMN_RE.match(inner) else UNKNOWN_DAY_ORIGIN
    base = inner[:intervals[0].start()].strip()
    if not _BARE_COLUMN_RE.match(base):
        return UNKNOWN_DAY_ORIGIN
    # Everything after the base column must be one contiguous chain of signed
    # constant INTERVAL terms; anything else is undecidable rather than zero.
    cursor = intervals[0].start()
    signed_shift = 0
    for interval in intervals:
        if interval.start() != cursor:
            return UNKNOWN_DAY_ORIGIN
        amount = int(interval.group("value"))
        unit = interval.group("unit").lower()
        unit_minutes = 60 if unit in {"hour", "hours"} else 1
        signed_shift += amount * unit_minutes * (
            1 if interval.group("sign") == "+" else -1
        )
        cursor = interval.end()
    if inner[cursor:].strip():
        return UNKNOWN_DAY_ORIGIN
    # ``ts - INTERVAL 'n'`` moves each day boundary forward by n minutes, so
    # the business-day origin is the negation of the applied signed shift.
    # A positive SQL shift yields a negative origin that wraps to the same
    # point in the previous day; multi-day shifts stay undecidable.
    if abs(signed_shift) >= _DAY_MINUTES:
        return UNKNOWN_DAY_ORIGIN
    return (-signed_shift) % _DAY_MINUTES


def extract_day_origin_minutes(group_by: tuple[str, ...] | list[str]) -> int | None:
    """Extract the business-day start (minutes after UTC midnight) of day buckets.

    Returns ``None`` when the GROUP BY does not bucket by day, a value in
    ``0..1439`` when every day bucket shares one constant origin, or
    :data:`UNKNOWN_DAY_ORIGIN` when a day bucket exists but its origin cannot
    be statically decided (e.g. dynamic or non-constant offsets).
    """
    if detect_granularity(group_by) != "day":
        return None
    origins: set[int] = set()
    for item in group_by:
        if detect_granularity((item,)) != "day":
            continue
        origins.add(_classify_day_item(item))
    if not origins:
        return UNKNOWN_DAY_ORIGIN
    if len(origins) > 1:
        return UNKNOWN_DAY_ORIGIN
    return origins.pop()


def detect_time_grain(group_by: tuple[str, ...] | list[str]) -> tuple[str, int | None]:
    """Return ``(granularity label, business-day origin in minutes)``."""
    granularity = detect_granularity(group_by)
    if granularity != "day":
        return granularity, None
    return granularity, extract_day_origin_minutes(group_by)


def sort_unique(values: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))
