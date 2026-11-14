"""sqlglot-backed parser. Imported lazily so it stays an optional dependency."""

from __future__ import annotations

import importlib

from .common import (
    extract_identifiers,
    line_range,
    strip_comments,
    top_level_words,
)
from .joins import (
    REASON_CROSS_JOIN,
    REASON_MULTI_JOIN,
    REASON_NON_COLUMN_ENDPOINT,
    REASON_NON_EQUALITY,
    REASON_NO_ON_CLAUSE,
    REASON_OR_PREDICATE,
    REASON_SUBQUERY,
    REASON_UNKNOWN_ALIAS,
    REASON_UNQUALIFIED_COLUMN,
    REASON_UNSUPPORTED_JOIN_TYPE,
)
from .model import (
    Aggregate,
    ColumnRef,
    JoinEquality,
    JoinSpec,
    ParsedQuery,
    canonical_expr,
    detect_granularity,
    sort_unique,
)


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


def _undecidable_join(join_type: str, left_alias: str, left_table: str,
                      right_alias: str, right_table: str,
                      reasons: list[str]) -> JoinSpec:
    return JoinSpec(
        join_type=join_type,
        left_alias=left_alias,
        left_table=left_table,
        right_alias=right_alias,
        right_table=right_table,
        equalities=(),
        decidable=False,
        undecidable_reasons=tuple(dict.fromkeys(reasons)),
    )


def _flatten_and(node):
    name = type(node).__name__
    if name == "And":
        yield from _flatten_and(node.this)
        yield from _flatten_and(node.expression)
    elif name == "Paren":
        yield from _flatten_and(node.this)
    else:
        yield node


def _column_endpoint(node, bindings: dict[str, str], left_alias: str, right_alias: str):
    if type(node).__name__ != "Column":
        return None, REASON_NON_COLUMN_ENDPOINT
    alias = (node.table or "").lower()
    column = (node.name or "").lower()
    if not alias:
        return None, REASON_UNQUALIFIED_COLUMN
    table = bindings.get(alias)
    if table is None:
        return None, REASON_UNKNOWN_ALIAS
    return ColumnRef(alias=alias, table=table, column=column), None


def _extract_joins(parsed, exp) -> tuple[JoinSpec, ...]:
    from_node = parsed.find(exp.From)
    join_nodes = parsed.args.get("joins") or []
    if from_node is None or not join_nodes:
        return ()
    base = from_node.this
    if not isinstance(base, exp.Table) or not all(isinstance(node.this, exp.Table)
                                                  for node in join_nodes):
        return (_undecidable_join("", "", "", "", "", [REASON_SUBQUERY]),)
    if len(join_nodes) != 1:
        return (_undecidable_join("", "", "", "", "", [REASON_MULTI_JOIN]),)

    join_node = join_nodes[0]
    left_alias = (base.alias or base.name).lower()
    left_table = base.name.lower()
    right_alias = (join_node.this.alias or join_node.this.name).lower()
    right_table = join_node.this.name.lower()
    side = (join_node.side or "").upper()
    kind = (join_node.kind or "").upper()
    method = (join_node.args.get("method") or "").upper()
    # A bare join node without side/kind/on is how sqlglot renders "a, b".
    if not side and not kind and not method \
            and join_node.args.get("on") is None \
            and join_node.args.get("using") is None:
        return (_undecidable_join("", "", "", "", "", [REASON_CROSS_JOIN]),)
    if method == "NATURAL":
        return (_undecidable_join("natural", "", "", "", "",
                                  [REASON_UNSUPPORTED_JOIN_TYPE]),)
    if kind == "CROSS":
        return (_undecidable_join("", "", "", "", "", [REASON_CROSS_JOIN]),)
    if side not in {"", "INNER", "LEFT"}:
        return (_undecidable_join((side or kind or "").lower(), "", "", "", "",
                                  [REASON_UNSUPPORTED_JOIN_TYPE]),)
    join_type = "left" if side == "LEFT" else "inner"

    on_node = join_node.args.get("on")
    if on_node is None:
        # USING / NATURAL joins omit an explicit ON predicate and therefore
        # expose no checkable qualified endpoints in this bounded subset.
        return (_undecidable_join(join_type, "", "", "", "",
                                  [REASON_NO_ON_CLAUSE]),)
    bindings = {left_alias: left_table, right_alias: right_table}
    reasons: list[str] = []
    equalities: list[JoinEquality] = []
    for conjunct in _flatten_and(on_node):
        if type(conjunct).__name__ == "Or":
            reasons.append(REASON_OR_PREDICATE)
            continue
        if type(conjunct).__name__ != "EQ":
            reasons.append(REASON_NON_EQUALITY)
            continue
        left_ref, left_error = _column_endpoint(conjunct.this, bindings,
                                                 left_alias, right_alias)
        right_ref, right_error = _column_endpoint(conjunct.expression, bindings,
                                                   left_alias, right_alias)
        if left_error or right_error:
            reasons.append(left_error or right_error)
            continue
        if left_ref.alias == right_alias and right_ref.alias == left_alias:
            left_ref, right_ref = right_ref, left_ref
        elif left_ref.alias != left_alias or right_ref.alias != right_alias:
            reasons.append(REASON_NON_COLUMN_ENDPOINT)
            continue
        equalities.append(JoinEquality(left=left_ref, right=right_ref))
    if reasons or not equalities:
        if not equalities and not reasons:
            reasons.append(REASON_NON_EQUALITY)
        return (_undecidable_join(join_type, left_alias, left_table,
                                  right_alias, right_table, reasons),)
    return (JoinSpec(
        join_type=join_type,
        left_alias=left_alias,
        left_table=left_table,
        right_alias=right_alias,
        right_table=right_table,
        equalities=tuple(sorted(
            equalities,
            key=lambda item: (item.left.column, item.right.column),
        )),
        decidable=True,
    ),)


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
    joins = _extract_joins(parsed, exp)
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
        joins=joins,
    )
