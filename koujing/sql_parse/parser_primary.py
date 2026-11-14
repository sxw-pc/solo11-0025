"""sqlglot-backed parser. Imported lazily so it stays an optional dependency."""

from __future__ import annotations

import importlib

from .common import (
    extract_identifiers,
    line_range,
    strip_comments,
    top_level_words,
)
from .model import Aggregate, ParsedQuery, canonical_expr, detect_granularity, sort_unique


class PrimaryParserUnavailable(RuntimeError):
    """sqlglot is not installed."""


class PrimaryParserError(RuntimeError):
    """sqlglot was available but could not parse the statement."""


def _import_sqlglot():
    try:
        return importlib.import_module("sqlglot")
    except (ImportError, ModuleNotFoundError) as exc:
        raise PrimaryParserUnavailable("sqlglot is not installed") from exc


def _aggregate_of(inner, exp) -> Aggregate | None:
    classes = {
        "Count": "count",
        "Sum": "sum",
        "Avg": "avg",
        "Max": "max",
        "Min": "min",
    }
    function = classes.get(type(inner).__name__)
    if function is None:
        return None
    argument_node = inner.this if inner.this is not None else None
    if type(argument_node).__name__ == "Distinct":
        function = "count_distinct" if function == "count" else function
        expressions = argument_node.expressions
        argument_node = expressions[0] if expressions else None
    argument = argument_node.sql() if argument_node is not None else ""
    return Aggregate(
        function=function,
        expression=canonical_expr(argument),
        alias="",
        fields=extract_identifiers(argument),
    )


def _columns_in(node, exp) -> set[str]:
    if node is None:
        return set()
    found: set[str] = set()
    for column in node.find_all(exp.Column):
        name = column.name
        if name:
            found.add(name.lower())
    return found


def _warnings(clean: str, aggregates: tuple[Aggregate, ...], tables: tuple[str, ...]) -> tuple[str, ...]:
    warnings: list[str] = []
    unsupported = {"qualify", "window", "emit", "match_recognize", "pivot", "unpivot", "connect"}
    found = sorted({word.value for word in top_level_words(clean) if word.value in unsupported})
    warnings.extend(f"unsupported clause ignored: {word.upper()}" for word in found)
    if not tables:
        warnings.append("no base table recognised")
    if not aggregates:
        warnings.append("no direct aggregate projection recognised")
    for aggregate in aggregates:
        if not aggregate.alias:
            warnings.append(f"aggregate {aggregate.function} has no alias; no metric candidate")
    return tuple(dict.fromkeys(warnings))


def parse_primary(sql: str, *, dialect: str | None = None) -> ParsedQuery:
    """Parse with sqlglot and normalise to :class:`ParsedQuery`."""
    sqlglot = _import_sqlglot()
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # sqlglot uses several exception classes
        raise PrimaryParserError(f"sqlglot parse error: {exc}") from exc
    if parsed is None:
        raise PrimaryParserError("sqlglot returned no statement")
    exp = sqlglot.exp
    if not isinstance(parsed, exp.Select):
        select = parsed.find(exp.Select)
        if select is None:
            raise PrimaryParserError("statement is not a SELECT")
        parsed = select

    table_names = {
        table.name.lower()
        for table in parsed.find_all(exp.Table)
        if table.name and not isinstance(table.parent, exp.CTE)
    }
    aggregates: list[Aggregate] = []
    columns: set[str] = set()
    for projection in parsed.expressions:
        alias_node = projection if isinstance(projection, exp.Alias) else None
        inner = projection.this if alias_node is not None else projection
        aggregate = _aggregate_of(inner, exp)
        if aggregate is not None:
            alias = alias_node.alias if alias_node is not None else ""
            aggregate = Aggregate(
                function=aggregate.function,
                expression=aggregate.expression,
                alias=(alias or inner.alias).lower(),
                fields=aggregate.fields,
            )
            aggregates.append(aggregate)
        columns |= _columns_in(projection, exp)

    where_node = parsed.args.get("where")
    if where_node is not None:
        columns |= _columns_in(where_node, exp)
    group_node = parsed.args.get("group")
    if group_node is not None:
        columns |= _columns_in(group_node, exp)

    filter_expr = canonical_expr(where_node.this.sql()) if where_node is not None else ""
    group_by = ()
    if group_node is not None:
        group_by = tuple(sorted({
            canonical_expr(expression.sql()) for expression in group_node.expressions
        }))
    clean = strip_comments(sql)
    start_line, end_line = line_range(sql)
    ordered_aggregates = tuple(aggregates)
    return ParsedQuery(
        source="primary",
        tables=sort_unique(table_names),
        select_fields=sort_unique(columns),
        aggregates=ordered_aggregates,
        filter_expr=filter_expr,
        group_by=group_by,
        granularity=detect_granularity(group_by),
        warnings=_warnings(clean, ordered_aggregates, tuple(sorted(table_names))),
        line_start=start_line,
        line_end=end_line,
    )
