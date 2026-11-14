"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class RatioPart:
    """One side of a derived ratio.

    ``aggregation`` is empty when the operand could not be reduced to a
    supported aggregate/row expression. An unsupported part is kept verbatim
    in ``expression`` so the comparison layer can report ``undetermined``
    instead of silently dropping the derived alias.
    """

    aggregation: str
    expression: str
    fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        return self.aggregation in SUPPORTED_PART_AGGREGATIONS

    def to_dict(self) -> dict:
        return {
            "aggregation": self.aggregation,
            "expression": self.expression,
            "fields": list(self.fields),
            "supported": self.supported,
        }


SUPPORTED_PART_AGGREGATIONS = frozenset({
    "sum", "count", "count_distinct", "avg", "max", "min", "row",
})


@dataclass(frozen=True)
class RatioSpec:
    """Parser-neutral composite structure for a derived ratio projection.

    ``population_ratio`` covers ``SUM(x) / SUM(y)`` style ratios: numerator
    and denominator are aggregates applied independently. ``average_ratio``
    covers ``AVG(x / y)``: the ratio is formed per row and then averaged, which
    is deliberately a different kind from ``AVG(x) / AVG(y)``.
    """

    kind: str
    numerator: RatioPart
    denominator: RatioPart
    zero_division: str
    expression: str
    unsupported: tuple[str, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        return (not self.unsupported
                and self.numerator.supported
                and self.denominator.supported)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "numerator": self.numerator.to_dict(),
            "denominator": self.denominator.to_dict(),
            "zero_division": self.zero_division,
            "expression": self.expression,
            "undetermined": list(self.unsupported),
        }


@dataclass(frozen=True)
class Aggregate:
    """One aggregate projection such as ``SUM(pay_amount) AS order_gmv``."""

    function: str
    expression: str
    alias: str
    fields: tuple[str, ...] = field(default_factory=tuple)
    ratio: RatioSpec | None = None

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "expression": self.expression,
            "alias": self.alias,
            "fields": list(self.fields),
            "ratio": self.ratio.to_dict() if self.ratio is not None else None,
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
    return value


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
