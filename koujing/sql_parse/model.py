"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


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
    """The stable subset of a SELECT statement this project understands.

    ``select_fields`` is the flat union kept for audit compatibility.
    ``dimension_fields`` is the contract used by comparison: it contains only
    columns of non-aggregate projections, so an aggregate's own argument
    columns are never attributed to a sibling aggregate. Filter and GROUP BY
    identifiers are intentionally *not* folded in here; they are report-level
    conditions shared by every aggregate in the statement.
    """

    source: str
    tables: tuple[str, ...]
    select_fields: tuple[str, ...]
    aggregates: tuple[Aggregate, ...]
    filter_expr: str
    group_by: tuple[str, ...]
    granularity: str
    warnings: tuple[str, ...]
    line_start: int
    line_end: int
    dimension_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aggregates"] = [agg.to_dict() for agg in self.aggregates]
        return data

    def comparable(self) -> dict:
        """Fields that must agree between primary and fallback parsers."""
        return {
            "tables": list(self.tables),
            "select_fields": list(self.select_fields),
            "dimension_fields": list(self.dimension_fields),
            "aggregates": [agg.to_dict() for agg in self.aggregates],
            "filter_expr": self.filter_expr,
            "group_by": list(self.group_by),
            "granularity": self.granularity,
        }


def normalise_alias(alias: str | None) -> str:
    """Canonical form used for alias lookup and duplicate detection.

    Both parsers must agree: surrounding whitespace, quoting characters
    (backtick, double quote, square brackets), and letter case are cosmetic
    in the supported SQL subset.
    """
    if not alias:
        return ""
    value = str(alias).strip()
    quotes = {"`": "`", '"': '"', "[": "]", "'": "'"}
    if len(value) >= 2 and value[0] in quotes and value[-1] == quotes[value[0]]:
        value = value[1:-1].strip()
    return value.strip().lower()


def duplicate_alias_warnings(aggregates: tuple[Aggregate, ...] | list[Aggregate]) -> tuple[str, ...]:
    """Warn about aggregates that collapse to the same normalised alias.

    Duplicate aliases must not silently drop a projection. Each aggregate is
    still compared independently; the warning makes the ambiguity auditable.
    """
    counts: dict[str, int] = {}
    for aggregate in aggregates:
        alias = normalise_alias(aggregate.alias)
        if alias:
            counts[alias] = counts.get(alias, 0) + 1
    return tuple(
        f"duplicate aggregate alias {alias!r} used by {count} projections"
        for alias, count in sorted(counts.items())
        if count > 1
    )


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
        placeholder = f"\x02{index}\x02"
        position = value.find(placeholder)
        prefix = value[max(0, position - 48):position] if position >= 0 else ""
        # Date-part literals passed to date-truncation functions are case-insensitive
        # ('MONTH' vs 'month'); fold them so both parsers agree. Literals in ordinary
        # predicates stay verbatim because there they may be case-sensitive values.
        if _DATE_FUNCTION_RE.search(prefix):
            value = value.replace(placeholder, _fold_date_part(literal))
        else:
            value = value.replace(placeholder, literal)
    return value


_DATE_PART_LITERAL_RE = re.compile(
    r"^'((?:quarter|month|week|year|day|hour|minute|second))'$", re.I
)
_DATE_FUNCTION_RE = re.compile(
    r"(?:date_trunc|timestamp_trunc|time_trunc|date_part|trunc)\s*\(\s*$", re.I
)


def _fold_date_part(literal: str) -> str:
    match = _DATE_PART_LITERAL_RE.match(literal)
    if match is None:
        return literal
    return f"'{match.group(1).lower()}'"


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
        ("day", ("date(", "date (", "day(", "day (", "date_trunc('day'", 'date_trunc("day"')),
    )
    for label, needles in checks:
        if any(needle in joined for needle in needles):
            return label
    return "row"


def sort_unique(values: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))
