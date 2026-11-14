"""sqlglot-backed parser. Imported lazily so it stays an optional dependency."""

from __future__ import annotations

import importlib

from .common import (
    extract_identifiers,
    line_range,
    strip_comments,
    top_level_words,
)
from .lineage import (
    DERIVED_ALIAS,
    SCALAR_ALIAS,
    Ref,
    Rel,
    Scope,
    build_lineage,
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


# ---------------------------------------------------------------------------
# Scope-graph construction from the sqlglot AST (mirrors the fallback graph)
# ---------------------------------------------------------------------------

def _refs_of(node, exp) -> list[Ref]:
    if node is None:
        return []
    return [
        Ref(column.table.lower() or None, column.name.lower())
        for column in node.find_all(exp.Column)
        if column.name
    ]


def _select_branches(select_node, exp) -> list:
    """Flatten UNION branches inside a (possibly recursive) CTE body."""
    node = select_node
    if isinstance(node, exp.Union):
        return [node.this, node.expression]
    return [node]


def _build_scope_primary(select_node, exp, visible_ctes: dict[str, Scope],
                         cte_name: str | None) -> Scope:
    branches = _select_branches(select_node, exp)
    if len(branches) > 1:
        merged = Scope(cte_name=cte_name, ctes=dict(visible_ctes))
        for branch in branches:
            part = _build_scope_primary(branch, exp, visible_ctes, cte_name)
            for name, refs in part.outputs.items():
                merged.outputs.setdefault(name, []).extend(refs)
            merged.rels.extend(part.rels)
            merged.used_refs.extend(part.used_refs)
            merged.star = merged.star or part.star
            for key, value in part.ctes.items():
                merged.ctes.setdefault(key, value)
        return merged

    scope = Scope(cte_name=cte_name, ctes=dict(visible_ctes))

    with_node = select_node.args.get("with_") or select_node.args.get("with")
    if with_node is not None:
        for cte in with_node.expressions:
            name = cte.alias.lower()
            # Populate in place so recursive self references resolve to the
            # same Scope object the resolver cycle guard tracks.
            placeholder = Scope(cte_name=name, ctes=dict(scope.ctes))
            scope.ctes[name] = placeholder
            built = _build_scope_primary(cte.this, exp, scope.ctes, name)
            placeholder.rels = built.rels
            placeholder.outputs = built.outputs
            placeholder.used_refs = built.used_refs
            placeholder.ctes = built.ctes
            placeholder.star = built.star

    # Projection outputs and references.
    for projection in select_node.expressions:
        if isinstance(projection, exp.Star):
            scope.star = True
            continue
        alias = projection.alias.lower()
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        refs = _refs_of(inner, exp)
        if alias:
            scope.outputs.setdefault(alias, []).extend(refs)
        elif isinstance(inner, exp.Column) and inner.name:
            scope.outputs.setdefault(inner.name.lower(), []).extend(refs)
        scope.used_refs.extend(refs)

    # FROM / JOIN relations (only the immediate scope's nodes).
    from_node = select_node.args.get("from_") or select_node.args.get("from")
    derived_index = 0
    if from_node is not None:
        if isinstance(from_node.this, exp.Subquery):
            derived_index += 1
        scope.rels.append(_rel_primary(from_node.this, exp, scope.ctes, derived_index))
    for join in select_node.args.get("joins") or []:
        if isinstance(join.this, exp.Subquery):
            derived_index += 1
        scope.rels.append(_rel_primary(join.this, exp, scope.ctes, derived_index))
        on_node = join.args.get("on")
        if on_node is not None:
            scope.used_refs.extend(_refs_of(on_node, exp))
        using = join.args.get("using")
        if using is not None:
            scope.used_refs.extend(_refs_of(using, exp))

    # Scalar / IN / EXISTS subqueries in WHERE and HAVING.
    where_node = select_node.args.get("where")
    having_node = select_node.args.get("having")
    counter = 0
    for clause_node in (where_node, having_node):
        if clause_node is None:
            continue
        clause_refs = _refs_of(clause_node, exp)
        subqueries = [node for node in clause_node.find_all(exp.Subquery)]
        for subquery in subqueries:
            counter += 1
            alias = f"{SCALAR_ALIAS}:{counter}"
            child = _build_scope_primary(subquery.this, exp, scope.ctes, None)
            scope.rels.append(Rel(kind="derived", alias=alias, name=alias, scope=child))
            for ref in _refs_of(subquery, exp):
                if ref in clause_refs:
                    clause_refs.remove(ref)
        scope.used_refs.extend(clause_refs)

    group_node = select_node.args.get("group")
    if group_node is not None:
        scope.used_refs.extend(_refs_of(group_node, exp))
    return scope


def _rel_primary(node, exp, visible_ctes: dict[str, Scope],
                 derived_index: int = 0) -> Rel:
    if isinstance(node, exp.Subquery):
        alias = node.alias.lower() or f"{DERIVED_ALIAS}:{derived_index or 1}"
        child = _build_scope_primary(node.this, exp, visible_ctes, None)
        return Rel(kind="derived", alias=alias, name=alias, scope=child)
    if isinstance(node, exp.Table):
        name = node.name.lower()
        alias = (node.alias or name).lower()
        if name in visible_ctes:
            return Rel(kind="cte", alias=alias, name=name, scope=visible_ctes[name])
        return Rel(kind="table", alias=alias, name=name, physical=name)
    # Parenthesised joins and other shapes degrade to unresolved relations.
    alias = (getattr(node, "alias", "") or "unknown").lower()
    return Rel(kind="derived", alias=alias, name=alias)


def _build_primary_graph(parsed, exp):
    select = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    cte_defs: list[tuple[str, Scope]] = []
    visible: dict[str, Scope] = {}
    warnings: list[str] = []
    with_node = parsed.args.get("with_") or parsed.args.get("with")
    if with_node is not None:
        if with_node.args.get("recursive"):
            warnings.append(
                "RECURSIVE keyword present; CTE self references stop at the anchor branch"
            )
        for cte in with_node.expressions:
            name = cte.alias.lower()
            placeholder = Scope(cte_name=name)
            visible[name] = placeholder
            built = _build_scope_primary(cte.this, exp, visible, name)
            placeholder.rels = built.rels
            placeholder.outputs = built.outputs
            placeholder.used_refs = built.used_refs
            placeholder.ctes = built.ctes
            placeholder.star = built.star
            cte_defs.append((name, placeholder))
    root = _build_scope_primary(select, exp, visible, None)
    return root, cte_defs, warnings


def _lineage_leaf_columns(field_lineage) -> tuple[str, ...]:
    leaves: set[str] = set()
    for item in field_lineage:
        if item.physical_tables:
            leaves.update(hop.column for hop in item.chain if hop.kind == "physical")
        else:
            leaves.add(item.column)
    return sort_unique(leaves)


def _outer_columns(node, exp) -> set[str]:
    """Column names visible at this scope, excluding nested subquery bodies."""
    if node is None:
        return set()
    found: set[str] = set()
    for column in node.find_all(exp.Column):
        # find_all descends into subqueries; skip columns whose nearest
        # enclosing Subquery sits below the given node.
        parent = column.parent
        inside_subquery = False
        while parent is not None and parent is not node:
            if isinstance(parent, exp.Subquery):
                inside_subquery = True
                break
            parent = parent.parent
        if not inside_subquery and column.name:
            found.add(column.name.lower())
    return found


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

    root, cte_defs, graph_warnings = _build_primary_graph(parsed, exp)
    tables, relations, ctes, field_lineage, lineage_warnings, resolver = build_lineage(
        root, cte_defs
    )

    aggregates: list[Aggregate] = []
    outer_columns: set[str] = set()
    for projection in parsed.expressions:
        alias_node = projection if isinstance(projection, exp.Alias) else None
        inner = projection.this if alias_node is not None else projection
        aggregate = _aggregate_of(inner, exp)
        if aggregate is not None:
            alias = alias_node.alias if alias_node is not None else ""
            refs = _refs_of(inner, exp)
            lineage = resolver.resolve((alias or inner.alias or "").lower(), refs)
            aggregate = Aggregate(
                function=aggregate.function,
                expression=aggregate.expression,
                alias=(alias or inner.alias).lower(),
                fields=aggregate.fields,
                lineage=lineage,
            )
            aggregates.append(aggregate)
        outer_columns |= _outer_columns(projection, exp)

    where_node = parsed.args.get("where")
    if where_node is not None:
        outer_columns |= _outer_columns(where_node, exp)
    group_node = parsed.args.get("group")
    if group_node is not None:
        outer_columns |= _outer_columns(group_node, exp)

    filter_expr = canonical_expr(where_node.this.sql()) if where_node is not None else ""
    group_by = ()
    if group_node is not None:
        group_by = tuple(sorted({
            canonical_expr(expression.sql()) for expression in group_node.expressions
        }))
    clean = strip_comments(sql)
    start_line, end_line = line_range(sql)
    ordered_aggregates = tuple(aggregates)
    lineage_leaves = set(_lineage_leaf_columns(field_lineage))
    select_fields = sort_unique(outer_columns | lineage_leaves)
    warnings = list(graph_warnings) + list(lineage_warnings) + list(resolver.warnings)
    warnings.extend(_warnings(clean, ordered_aggregates, tables))
    return ParsedQuery(
        source="primary",
        tables=tables,
        select_fields=select_fields,
        aggregates=ordered_aggregates,
        filter_expr=filter_expr,
        group_by=group_by,
        granularity=detect_granularity(group_by),
        warnings=tuple(dict.fromkeys(warnings)),
        line_start=start_line,
        line_end=end_line,
        relations=relations,
        ctes=ctes,
        field_lineage=field_lineage,
    )
