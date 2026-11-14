"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class LineageHop:
    """One hop of a column lineage chain.

    ``kind`` is ``physical`` for a real base table column, ``cte`` for a
    reference into a CTE scope and ``derived`` for a reference into a
    FROM/JOIN subquery (or nested scalar subquery) scope.
    """

    table: str
    column: str
    kind: str

    def to_dict(self) -> dict:
        return {"table": self.table, "column": self.column, "kind": self.kind}


@dataclass(frozen=True)
class ColumnLineage:
    """Traceable lineage for one outer column reference.

    ``chain`` walks the reference from the outer scope down to the physical
    source column(s). When a reference cannot be fully resolved,
    ``physical_tables`` is empty and the chain ends at the unresolved scope.
    """

    column: str
    physical_tables: tuple[str, ...]
    chain: tuple[LineageHop, ...]

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "physical_tables": list(self.physical_tables),
            "chain": [hop.to_dict() for hop in self.chain],
        }


@dataclass(frozen=True)
class RelationNode:
    """A scoped table reference inside a query's FROM/JOIN graph.

    ``kind`` is one of ``table`` (physical base table), ``cte`` (reference to
    a WITH-bound name) or ``derived`` (subquery). ``children`` holds the
    nested relations of a subquery or an expanded CTE body so the full scope
    tree is inspectable.
    """

    kind: str
    alias: str
    name: str
    physical_tables: tuple[str, ...]
    children: tuple["RelationNode", ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "alias": self.alias,
            "name": self.name,
            "physical_tables": list(self.physical_tables),
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class Aggregate:
    """One aggregate projection such as ``SUM(pay_amount) AS order_gmv``."""

    function: str
    expression: str
    alias: str
    fields: tuple[str, ...] = field(default_factory=tuple)
    lineage: tuple[ColumnLineage, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "expression": self.expression,
            "alias": self.alias,
            "fields": list(self.fields),
            "lineage": [item.to_dict() for item in self.lineage],
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
    relations: tuple[RelationNode, ...] = field(default_factory=tuple)
    ctes: tuple[RelationNode, ...] = field(default_factory=tuple)
    field_lineage: tuple[ColumnLineage, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aggregates"] = [agg.to_dict() for agg in self.aggregates]
        data["relations"] = [node.to_dict() for node in self.relations]
        data["ctes"] = [node.to_dict() for node in self.ctes]
        data["field_lineage"] = [item.to_dict() for item in self.field_lineage]
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
            "relations": [node.to_dict() for node in self.relations],
            "ctes": [node.to_dict() for node in self.ctes],
            "field_lineage": [item.to_dict() for item in self.field_lineage],
        }

    def lineage_for(self, column: str) -> tuple[ColumnLineage, ...]:
        """Return the recorded lineage entries whose outer column matches."""
        wanted = column.strip("`\"[]").lower()
        return tuple(item for item in self.field_lineage if item.column == wanted)


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
