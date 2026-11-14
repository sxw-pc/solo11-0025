"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ColumnRef:
    """A qualified column endpoint bound to a table alias.

    ``alias`` is the alias token actually written in the SQL (or the physical
    table name when the table has no explicit alias); ``table`` is the physical
    table that alias is bound to and ``column`` the physical column name.
    """

    alias: str
    table: str
    column: str

    def to_dict(self) -> dict:
        return {"alias": self.alias, "table": self.table, "column": self.column}


@dataclass(frozen=True)
class JoinEquality:
    """One conjunct ``left.column = right.column`` inside a JOIN ON clause."""

    left: ColumnRef
    right: ColumnRef

    def to_dict(self) -> dict:
        return {"left": self.left.to_dict(), "right": self.right.to_dict()}


@dataclass(frozen=True)
class JoinSpec:
    """The bounded, parser-neutral description of one two-table join.

    Only single-level, two-table INNER/LEFT joins with an equality-conjunct
    ON clause are described. Anything outside that subset leaves
    ``decidable`` False and records ``undecidable_reasons`` instead of
    pretending the relationship is known.
    """

    join_type: str
    left_alias: str
    left_table: str
    right_alias: str
    right_table: str
    equalities: tuple[JoinEquality, ...]
    decidable: bool
    undecidable_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "join_type": self.join_type,
            "left_alias": self.left_alias,
            "left_table": self.left_table,
            "right_alias": self.right_alias,
            "right_table": self.right_table,
            "equalities": [item.to_dict() for item in self.equalities],
            "decidable": self.decidable,
            "undecidable_reasons": list(self.undecidable_reasons),
        }


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
    warnings: tuple[str, ...]
    line_start: int
    line_end: int
    joins: tuple[JoinSpec, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aggregates"] = [agg.to_dict() for agg in self.aggregates]
        data["joins"] = [join.to_dict() for join in self.joins]
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
            "joins": [join.to_dict() for join in self.joins],
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
