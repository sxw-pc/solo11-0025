"""Standard-library SQL subset parser used when sqlglot is unavailable."""

from __future__ import annotations

import re

from .common import extract_identifiers, line_range, split_top_level, strip_comments, top_level_words
from .lineage import (
    build_fallback_graph,
    build_lineage,
    extract_refs,
)
from .model import (
    Aggregate,
    ColumnLineage,
    ParsedQuery,
    canonical_expr,
    detect_granularity,
    sort_unique,
)

AGGREGATE_RE = re.compile(
    r"^(count|sum|avg|max|min)\s*\((.*)\)\s*(?:(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?\s*$",
    re.I | re.S,
)
DISTINCT_RE = re.compile(r"^distinct\s+(.*)$", re.I | re.S)
UNSUPPORTED = {"qualify", "window", "emit", "match_recognize", "pivot", "unpivot", "connect"}


def _section_end(words: list, start_index: int, allowed: set[str]) -> int:
    for word in words[start_index:]:
        if word.value in allowed:
            return word.start
    return -1


def _aggregate_from_item(item: str) -> Aggregate | None:
    match = AGGREGATE_RE.match(item.strip())
    if not match:
        return None
    function = match.group(1).lower()
    raw_argument = match.group(2).strip()
    alias = (match.group(3) or "").lower()
    distinct = DISTINCT_RE.match(raw_argument)
    if function == "count" and distinct:
        function = "count_distinct"
        raw_argument = distinct.group(1).strip()
    return Aggregate(
        function=function,
        expression=canonical_expr(raw_argument),
        alias=alias,
        fields=extract_identifiers(raw_argument),
    )


def _aggregate_argument_refs(item: str) -> list:
    """Qualified refs from the raw aggregate argument (pre-canonicalisation)."""
    match = AGGREGATE_RE.match(item.strip())
    if not match:
        return []
    argument = match.group(2).strip()
    distinct = DISTINCT_RE.match(argument)
    if distinct:
        argument = distinct.group(1).strip()
    return extract_refs(argument)


def _strip_implicit_alias(item: str) -> str:
    match = re.match(r"^(.*\))\s+[A-Za-z_][A-Za-z0-9_]*\s*$", item.strip(), re.S)
    return match.group(1) if match else item


def _select_parts(select_clause: str) -> tuple[list[tuple[Aggregate, list]], tuple[str, ...]]:
    aggregates: list[tuple[Aggregate, list]] = []
    fields: list[str] = []
    for item in split_top_level(select_clause):
        aggregate = _aggregate_from_item(item)
        if aggregate is not None:
            aggregates.append((aggregate, _aggregate_argument_refs(item)))
            fields.extend(aggregate.fields)
        else:
            fields.extend(extract_identifiers(_strip_implicit_alias(item)))
    return aggregates, sort_unique(fields)


def _extract_query_text(sql: str) -> dict[str, str]:
    words = top_level_words(sql)
    select_index = next((i for i, word in enumerate(words) if word.value == "select"), -1)
    if select_index < 0:
        return {}
    from_index = next((i for i in range(select_index + 1, len(words))
                       if words[i].value == "from"), -1)
    if from_index < 0:
        return {"select": sql[words[select_index].end:].strip()}
    where_index = next((i for i in range(from_index + 1, len(words))
                        if words[i].value == "where"), -1)
    sections: dict[str, str] = {}
    select_end = words[from_index].start
    sections["select"] = sql[words[select_index].end:select_end].strip()
    stop_after_from = {"where", "group", "having", "order", "limit", "union"}
    from_end = _section_end(words, from_index + 1, stop_after_from)
    sections["from"] = sql[words[from_index].end:from_end if from_end >= 0 else len(sql)].strip()
    if where_index >= 0:
        where_end = _section_end(words, where_index + 1,
                                 {"group", "having", "order", "limit", "union"})
        sections["where"] = sql[words[where_index].end:
                                where_end if where_end >= 0 else len(sql)].strip()
    group_index = -1
    for index in range(select_index + 1, len(words) - 1):
        if words[index].value == "group" and words[index + 1].value == "by":
            group_index = index
            break
    if group_index >= 0:
        group_end = _section_end(words, group_index + 2,
                                 {"having", "order", "limit", "union", "window", "qualify"})
        sections["group"] = sql[words[group_index + 1].end:
                                group_end if group_end >= 0 else len(sql)].strip()
    return sections


def _lineage_leaf_columns(field_lineage: tuple[ColumnLineage, ...]) -> tuple[str, ...]:
    leaves: set[str] = set()
    for item in field_lineage:
        if item.physical_tables:
            leaves.update(hop.column for hop in item.chain if hop.kind == "physical")
        else:
            leaves.add(item.column)
    return sort_unique(leaves)


def parse_fallback(sql: str, *, source: str = "fallback",
                   extra_warnings: tuple[str, ...] = ()) -> ParsedQuery:
    """Parse a SELECT statement without executing it or using any database."""
    clean = strip_comments(sql)
    start_line, end_line = line_range(sql)
    warnings: list[str] = list(extra_warnings)
    sections = _extract_query_text(clean)
    if not sections:
        return ParsedQuery(
            source=source,
            tables=(),
            select_fields=(),
            aggregates=(),
            filter_expr="",
            group_by=(),
            granularity="none",
            warnings=tuple(warnings + ["no SELECT statement found"]),
            line_start=start_line,
            line_end=end_line,
        )

    if "from" not in sections:
        warnings.append("no FROM clause found; table check skipped")
    if clean.count("(") != clean.count(")"):
        warnings.append("unbalanced parentheses in SQL")
    words = top_level_words(clean)
    found_unsupported = sorted({word.value for word in words if word.value in UNSUPPORTED})
    warnings.extend(f"unsupported clause ignored: {word.upper()}" for word in found_unsupported)

    # Build the scoped relation graph and resolve physical lineage.
    root, cte_defs, graph_warnings = build_fallback_graph(clean)
    warnings.extend(graph_warnings)
    tables, relations, ctes, field_lineage, lineage_warnings, resolver = build_lineage(
        root, cte_defs
    )
    warnings.extend(lineage_warnings)

    aggregate_parts, raw_select_fields = _select_parts(sections.get("select", ""))
    # Aggregate lineage is traced from the aggregate's own raw argument
    # expression (qualifiers preserved); ON / WHERE / GROUP BY columns
    # belong to the query-wide field lineage.
    enriched_aggregates: list[Aggregate] = []
    for aggregate, refs in aggregate_parts:
        lineage = resolver.resolve(aggregate.alias, refs)
        enriched_aggregates.append(Aggregate(
            function=aggregate.function,
            expression=aggregate.expression,
            alias=aggregate.alias,
            fields=aggregate.fields,
            lineage=lineage,
        ))

    where_expr = canonical_expr(sections.get("where", ""))
    group_exprs = tuple(
        sorted({canonical_expr(item) for item in split_top_level(sections.get("group", "")) if item})
    )
    lineage_leaves = set(_lineage_leaf_columns(field_lineage))
    # Columns referenced in the outer select but absent from the graph (e.g.
    # literal-only projections) keep their identifier-based names.
    select_fields = sort_unique(set(raw_select_fields) | lineage_leaves)
    if not tables:
        warnings.append("no base table recognised")
    if not enriched_aggregates:
        warnings.append("no direct aggregate projection recognised")
    for aggregate in enriched_aggregates:
        if not aggregate.alias:
            warnings.append(f"aggregate {aggregate.function} has no alias; no metric candidate")
    # Warnings produced while tracing aggregate lineage (e.g. recursive CTEs).
    warnings.extend(resolver.warnings)
    return ParsedQuery(
        source=source,
        tables=tables,
        select_fields=select_fields,
        aggregates=tuple(enriched_aggregates),
        filter_expr=where_expr,
        group_by=group_exprs,
        granularity=detect_granularity(group_exprs),
        warnings=tuple(dict.fromkeys(warnings)),
        line_start=start_line,
        line_end=end_line,
        relations=relations,
        ctes=ctes,
        field_lineage=field_lineage,
    )
