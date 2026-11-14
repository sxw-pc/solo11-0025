"""Scoped table-reference graph and column-lineage resolution.

Both parsers build the same lightweight scope graph -- the fallback parser
constructs it directly from SQL text, the primary parser walks the sqlglot
AST -- and a single resolver then traces every column reference through CTE
bodies, FROM/JOIN derived tables and nested subqueries down to physical base
tables and their columns.

Scoping rules enforced here:

* a CTE name is only visible inside the statement that defines it;
* a derived-table alias only exists in the scope that owns the subquery;
* a CTE or derived alias that happens to equal a physical table name shadows
  that table inside its own scope and never leaks out;
* recursive CTE self references are followed up to the anchor branch and then
  stop, so resolution can never loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import SQL_KEYWORDS
from .common import (
    clean_identifier,
    split_top_level,
    strip_comments,
    strip_string_literals,
    top_level_words,
)
from .model import ColumnLineage, LineageHop, RelationNode, sort_unique

DERIVED_ALIAS = "@derived"
SCALAR_ALIAS = "@scalar"

# A qualified (``a.b``) or bare (``b``) identifier. Function calls are
# excluded by the negative look-ahead on an opening parenthesis.
_REF_RE = re.compile(
    r"(?<![\w.])(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)(?!\w)(?!\s*\()"
)
_EXTRA_STOPWORDS = SQL_KEYWORDS | {"all", "recursive", "on", "join", "using"}
_JOIN_MODIFIERS = {"inner", "left", "right", "full", "cross", "outer"}
_CLAUSE_BOUNDARIES = {
    "select", "from", "where", "group", "having", "order", "limit",
    "union", "window", "qualify",
}


@dataclass
class Ref:
    """One column reference as written in a scope."""

    qualifier: str | None
    column: str


@dataclass
class Rel:
    """A relation bound in a scope's FROM/JOIN graph."""

    kind: str  # table | cte | derived
    alias: str
    name: str
    scope: "Scope | None" = None
    physical: str | None = None


@dataclass
class Scope:
    """One SELECT-level namespace."""

    cte_name: str | None = None
    rels: list[Rel] = field(default_factory=list)
    outputs: dict[str, list[Ref]] = field(default_factory=dict)
    used_refs: list[Ref] = field(default_factory=list)
    ctes: dict[str, "Scope"] = field(default_factory=dict)
    star: bool = False


def extract_refs(text: str) -> list[Ref]:
    """Extract column references from a SQL expression fragment."""
    if not text:
        return []
    cleaned = strip_string_literals(strip_comments(text))
    refs: list[Ref] = []
    for match in _REF_RE.finditer(cleaned):
        qualifier_text, column_text = match.group(1), match.group(2)
        column = column_text.lower()
        if qualifier_text is None:
            if column in _EXTRA_STOPWORDS:
                continue
            qualifier = None
        else:
            qualifier = qualifier_text.lower()
        refs.append(Ref(qualifier, column))
    return refs


# ---------------------------------------------------------------------------
# Text-level structural parsing (fallback parser)
# ---------------------------------------------------------------------------

def _match_paren(text: str, open_pos: int) -> int:
    """Return the position of the parenthesis matching ``text[open_pos]``."""
    depth = 0
    quote = ""
    index = open_pos
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text) - 1


def _strip_outer_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("("):
        close = _match_paren(text, 0)
        if close != len(text.rstrip()) - 1:
            break
        text = text[1:close].strip()
    return text


def _split_cte_list(sql: str) -> tuple[list[tuple[str, str]], str, bool]:
    """Split a top-level WITH clause into ``(name, body)`` pairs."""
    words = top_level_words(sql)
    if not words or words[0].value != "with":
        return [], sql, False
    cursor = words[0].end
    recursive = False
    if len(words) > 1 and words[1].value == "recursive":
        recursive = True
        cursor = words[1].end
    ctes: list[tuple[str, str]] = []
    while True:
        name_match = re.search(r"[A-Za-z_][\w$]*", sql[cursor:])
        if name_match is None:
            break
        name = clean_identifier(name_match.group(0))
        as_match = re.search(r"\bas\b", sql[cursor + name_match.end():], flags=re.I)
        if as_match is None:
            break
        paren_pos = sql.find("(", cursor + name_match.end() + as_match.end())
        if paren_pos < 0:
            break
        close = _match_paren(sql, paren_pos)
        ctes.append((name, sql[paren_pos + 1:close]))
        # A top-level comma continues the CTE list; anything else starts the
        # statement body. Whitespace and parenthesised content are skipped.
        depth = 0
        quote = ""
        index = close + 1
        while index < len(sql):
            char = sql[index]
            if quote:
                if char == quote:
                    quote = ""
            elif char in {"'", '"', "`"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and char == ",":
                cursor = index + 1
                break
            elif depth == 0 and (char.isalpha() or char == "_"):
                return ctes, sql[index:].strip(), recursive
            index += 1
        else:
            return ctes, "", recursive
    return [], sql, False


def _clause_spans(sql: str) -> dict[str, tuple[int, int]]:
    """Map outer-level clause names to their character spans."""
    words = top_level_words(sql)
    spans: dict[str, tuple[int, int]] = {}
    for index, word in enumerate(words):
        if word.value in _CLAUSE_BOUNDARIES and word.value not in spans:
            end = len(sql)
            for later in words[index + 1:]:
                if later.value in _CLAUSE_BOUNDARIES:
                    end = later.start
                    break
            spans[word.value] = (word.end, end)
    return spans


def _split_union_branches(text: str) -> list[str]:
    words = top_level_words(text)
    cuts = [word for word in words if word.value == "union"]
    if not cuts:
        return [text]
    branches: list[str] = []
    start = 0
    for word in cuts:
        branches.append(text[start:word.start])
        start = word.end
        tail = top_level_words(text[word.end:])
        if tail and tail[0].value in {"all", "distinct"}:
            start = word.end + tail[0].end
    branches.append(text[start:])
    return branches


def _split_alias(item: str) -> tuple[str, str]:
    """Split ``expression [AS] alias`` into expression and alias text."""
    stripped = item.strip()
    for match in re.finditer(r"\bas\b", stripped, flags=re.I):
        if stripped[:match.start()].count("(") == stripped[:match.start()].count(")"):
            alias_match = re.search(r"[A-Za-z_][\w$]*\s*$", stripped[match.end():])
            if alias_match:
                return stripped[:match.start()].strip(), clean_identifier(alias_match.group(0))
    bare = re.match(r"^(.*?)\s+([A-Za-z_][\w$]*)\s*$", stripped, flags=re.S)
    if bare and bare.group(2).lower() not in SQL_KEYWORDS:
        head = bare.group(1).strip()
        if "(" in head or re.fullmatch(r"[`\"\[\]\w.$]+", head):
            return head, clean_identifier(bare.group(2))
    return stripped, ""


def _outputs_of(select_text: str, scope: Scope) -> None:
    for item in split_top_level(select_text):
        if item == "*" or item.endswith(".*"):
            scope.star = True
            continue
        expression, alias = _split_alias(item)
        refs = extract_refs(expression)
        if alias:
            scope.outputs.setdefault(alias, []).extend(refs)
        elif re.fullmatch(
            r'(?:[A-Za-z_`"\[][\w$`"\]\[\.]*\s*\.\s*)?[`"\[]?[A-Za-z_][\w$]*[`"\]]?',
            expression,
        ):
            scope.outputs.setdefault(clean_identifier(expression), []).extend(refs)
        scope.used_refs.extend(refs)


def _split_joins(text: str) -> list[str]:
    words = top_level_words(text)
    cuts: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if word.value == "join":
            cut = word.start
            probe = index - 1
            while probe >= 0 and words[probe].value in _JOIN_MODIFIERS:
                cut = words[probe].start
                probe -= 1
            cuts.append((cut, word.end))
    if not cuts:
        return [text] if text.strip() else []
    parts: list[str] = []
    start = 0
    for cut, keyword_end in cuts:
        parts.append(text[start:cut])
        start = keyword_end
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _split_from_segments(from_text: str) -> list[str]:
    """Split a FROM clause at top-level commas and JOIN phrases."""
    comma_pieces = split_top_level(from_text)
    segments: list[str] = []
    for piece in comma_pieces:
        segments.extend(_split_joins(piece))
    return segments


def _strip_join_condition(segment: str) -> tuple[str, str]:
    """Separate a relation head from its ON / USING condition."""
    for word in top_level_words(segment):
        if word.value in {"on", "using"}:
            return segment[:word.start].strip(), segment[word.end:].strip()
    return segment.strip(), ""


def _head_alias(remainder: str) -> str:
    match = re.match(r"\s*(?:as\s+)?([`\"\[\]\w]+)", remainder, flags=re.I)
    return clean_identifier(match.group(1)) if match else ""


def _build_scope(text: str, visible_ctes: dict[str, Scope],
                 cte_name: str | None, depth: int) -> Scope:
    text = _strip_outer_parens(strip_comments(text).strip().rstrip(";").strip())
    branches = _split_union_branches(text)
    if len(branches) > 1:
        merged = Scope(cte_name=cte_name, ctes=dict(visible_ctes))
        for branch in branches:
            part = _build_scope(branch, visible_ctes, cte_name, depth)
            for name, refs in part.outputs.items():
                merged.outputs.setdefault(name, []).extend(refs)
            merged.rels.extend(part.rels)
            merged.used_refs.extend(part.used_refs)
            merged.star = merged.star or part.star
            for key, value in part.ctes.items():
                merged.ctes.setdefault(key, value)
        return merged

    scope = Scope(cte_name=cte_name, ctes=dict(visible_ctes))
    local_ctes, body, _recursive = _split_cte_list(text)
    for name, cte_body in local_ctes:
        # The placeholder is populated in place so recursive self references
        # keep pointing at the very Scope object that gets filled in.
        placeholder = Scope(cte_name=name, ctes=dict(scope.ctes))
        scope.ctes[name] = placeholder
        built = _build_scope(cte_body, scope.ctes, name, depth + 1)
        placeholder.rels = built.rels
        placeholder.outputs = built.outputs
        placeholder.used_refs = built.used_refs
        placeholder.ctes = built.ctes
        placeholder.star = built.star
    spans = _clause_spans(body)
    if "select" not in spans:
        return scope
    select_start, _ = spans["select"]
    from_keyword = next(
        (word for word in top_level_words(body) if word.value == "from"), None
    )
    select_end = from_keyword.start if from_keyword is not None else len(body)
    select_text = body[select_start:select_end]
    _outputs_of(select_text, scope)

    if from_keyword is not None:
        from_span = spans["from"]
        from_text = body[from_span[0]:from_span[1]]
        derived_counter = 0
        for segment in _split_from_segments(from_text):
            head, condition = _strip_join_condition(segment)
            scope.used_refs.extend(extract_refs(_mask_subqueries(condition)))
            if head.lstrip().startswith("("):
                derived_counter += 1
                scope.rels.append(
                    _build_rel(head, scope.ctes, depth, derived_counter)
                )
            else:
                scope.rels.append(_build_rel(head, scope.ctes, depth, 0))
            # Subqueries appearing inside ON conditions become child scopes.
            if "(" in condition:
                _attach_scalar_subqueries(condition, scope, depth)

    for clause in ("where", "having", "group"):
        span = spans.get(clause)
        if span:
            scope.used_refs.extend(
                _attach_scalar_subqueries(body[span[0]:span[1]], scope, depth)
            )
    return scope


def _mask_subqueries(text: str) -> str:
    """Blank out parenthesised SELECT subqueries, keeping outer refs only."""
    chars = list(text)
    index = 0
    while index < len(chars):
        if chars[index] != "(":
            index += 1
            continue
        close = _match_paren(text, index)
        if re.match(r"\s*select\b", text[index + 1:close], flags=re.I):
            for pos in range(index, close + 1):
                chars[pos] = " "
        index = close + 1
    return "".join(chars)


def _attach_scalar_subqueries(text: str, scope: Scope, depth: int) -> list[Ref]:
    """Attach nested SELECT subqueries inside an expression as child scopes."""
    counter = sum(1 for rel in scope.rels if rel.alias.startswith(SCALAR_ALIAS))
    index = 0
    while index < len(text):
        if text[index] != "(":
            index += 1
            continue
        close = _match_paren(text, index)
        inner = text[index + 1:close]
        if re.match(r"\s*select\b", inner, flags=re.I):
            counter += 1
            alias = f"{SCALAR_ALIAS}:{counter}"
            child = _build_scope(inner, scope.ctes, None, depth + 1)
            scope.rels.append(Rel(kind="derived", alias=alias, name=alias, scope=child))
        index = close + 1
    # Only references outside the subqueries belong to this scope.
    return extract_refs(_mask_subqueries(text))


def _build_rel(head: str, visible_ctes: dict[str, Scope], depth: int,
               derived_index: int = 0) -> Rel:
    head = head.strip()
    if head.startswith("("):
        close = _match_paren(head, 0)
        inner = head[1:close]
        alias = _head_alias(head[close + 1:]) or f"{DERIVED_ALIAS}:{derived_index or 1}"
        child = _build_scope(inner, visible_ctes, None, depth + 1)
        return Rel(kind="derived", alias=alias, name=alias, scope=child)
    token = head.split(None, 1)
    name = clean_identifier(token[0])
    alias = _head_alias(token[1]) if len(token) > 1 else ""
    if name in visible_ctes:
        return Rel(kind="cte", alias=alias or name, name=name,
                   scope=visible_ctes[name])
    return Rel(kind="table", alias=alias or name, name=name, physical=name)


def build_fallback_graph(sql: str) -> tuple[Scope, list[tuple[str, Scope]], list[str]]:
    """Build the scope graph for a statement using only the stdlib parser."""
    warnings: list[str] = []
    clean = strip_comments(sql)
    top_ctes, body, recursive = _split_cte_list(clean)
    if recursive:
        warnings.append(
            "RECURSIVE keyword present; CTE self references stop at the anchor branch"
        )
    cte_defs: list[tuple[str, Scope]] = []
    visible: dict[str, Scope] = {}
    for name, cte_body in top_ctes:
        placeholder = Scope(cte_name=name)
        visible[name] = placeholder  # visible to later CTEs and to itself
        built = _build_scope(cte_body, visible, name, 1)
        placeholder.rels = built.rels
        placeholder.outputs = built.outputs
        placeholder.used_refs = built.used_refs
        placeholder.ctes = built.ctes
        placeholder.star = built.star
        cte_defs.append((name, placeholder))
    root = _build_scope(body, visible, None, 0) if body.strip() else Scope(ctes=dict(visible))
    return root, cte_defs, warnings


# ---------------------------------------------------------------------------
# Shared lineage resolver
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Terminal:
    table: str
    column: str
    kind: str
    chain: tuple[LineageHop, ...]


class LineageResolver:
    """Traces references through an already-built scope graph."""

    def __init__(self, root: Scope, cte_defs: list[tuple[str, Scope]]):
        self.root = root
        self.cte_defs = cte_defs
        self.warnings: list[str] = []
        self._recursive_warned: set[str] = set()
        self._shadow_warned: set[str] = set()

    # -- public outputs ---------------------------------------------------

    def physical_tables(self) -> tuple[str, ...]:
        found: set[str] = set()

        def walk(scope: Scope, seen: set[int]) -> None:
            if id(scope) in seen:
                return
            seen.add(id(scope))
            for rel in scope.rels:
                if rel.kind == "table" and rel.physical:
                    found.add(rel.physical)
                elif rel.scope is not None:
                    walk(rel.scope, seen)

        walk(self.root, set())
        for _, scope in self.cte_defs:
            walk(scope, set())
        return sort_unique(found)

    def relation_tree(self, scope: Scope) -> tuple[RelationNode, ...]:
        return self._relation_tree(scope, set())

    def _relation_tree(self, scope: Scope, seen: frozenset | set[int]) -> tuple[RelationNode, ...]:
        if id(scope) in seen:
            return ()
        seen = set(seen) | {id(scope)}
        nodes: list[RelationNode] = []
        for rel in scope.rels:
            children = self._relation_tree(rel.scope, seen) if rel.scope is not None else ()
            nodes.append(RelationNode(
                kind=rel.kind,
                alias=rel.alias,
                name=rel.name,
                physical_tables=self._physical_of(rel),
                children=children,
            ))
        return tuple(nodes)

    def cte_tree(self) -> tuple[RelationNode, ...]:
        return tuple(
            RelationNode(
                kind="cte",
                alias=name,
                name=name,
                physical_tables=self._scope_physical(scope),
                children=self.relation_tree(scope),
            )
            for name, scope in self.cte_defs
        )

    def shadow_warnings(self, physical_tables: tuple[str, ...]) -> list[str]:
        physical = set(physical_tables)
        warnings: list[str] = []

        def walk(scope: Scope, seen: set[int]) -> None:
            if id(scope) in seen:
                return
            seen.add(id(scope))
            for rel in scope.rels:
                if rel.kind in {"cte", "derived"} and rel.name in physical \
                        and rel.name not in self._shadow_warned:
                    self._shadow_warned.add(rel.name)
                    label = "CTE" if rel.kind == "cte" else "derived table alias"
                    warnings.append(
                        f"{label} '{rel.name}' shadows a physical table name; "
                        "the scoped reference is kept separate"
                    )
                if rel.scope is not None:
                    walk(rel.scope, seen)

        walk(self.root, set())
        return warnings

    def resolve(self, outer_column: str, refs: list[Ref]) -> tuple[ColumnLineage, ...]:
        if not refs:
            return ()
        terminals: set[_Terminal] = set()
        for ref in refs:
            self._trace(self.root, ref, [], set(), terminals)
        ordered = sorted(
            terminals,
            key=lambda item: (
                item.table, item.column,
                tuple((hop.table, hop.column, hop.kind) for hop in item.chain),
            ),
        )
        return tuple(
            ColumnLineage(
                column=outer_column,
                physical_tables=(item.table,) if item.kind == "physical" else (),
                chain=item.chain,
            )
            for item in ordered
        )

    def field_lineage(self) -> tuple[ColumnLineage, ...]:
        grouped: dict[str, list[Ref]] = {}
        for ref in self.root.used_refs:
            grouped.setdefault(ref.column, []).append(ref)
        result: list[ColumnLineage] = []
        for column in sorted(grouped):
            result.extend(self.resolve(column, grouped[column]))
        return tuple(result)

    # -- internals --------------------------------------------------------

    def _trace(self, scope: Scope, ref: Ref, chain: list[LineageHop],
               seen: set[tuple[int, str]], terminals: set[_Terminal]) -> None:
        rels = self._find_rels(scope, ref)
        if not rels:
            terminals.add(_Terminal(
                "", ref.column, "unresolved",
                tuple(chain + [LineageHop(ref.qualifier or "?", ref.column, "unresolved")]),
            ))
            return
        for rel in rels:
            self._trace_rel(rel, scope, ref, chain, seen, terminals)

    def _trace_rel(self, rel: Rel, scope: Scope, ref: Ref,
                   chain: list[LineageHop], seen: set[tuple[int, str]],
                   terminals: set[_Terminal]) -> None:
        if rel.kind == "table":
            terminals.add(_Terminal(
                rel.physical or rel.name, ref.column, "physical",
                tuple(chain + [LineageHop(rel.physical or rel.name, ref.column, "physical")]),
            ))
            return
        child = rel.scope
        hop = LineageHop(rel.alias, ref.column, rel.kind)
        if child is not None and child.cte_name:
            self._recursive_warning(child.cte_name)
        guard = (id(child), ref.column)
        if guard in seen or child is None:
            # Mark the self reference explicitly so the chain shows where
            # expansion stopped instead of silently ending on a CTE hop.
            stop = LineageHop(child.cte_name if child is not None else rel.name,
                              ref.column, "unresolved")
            terminals.add(_Terminal("", ref.column, "unresolved",
                                    tuple(chain + [hop, stop])))
            return
        next_seen = seen | {guard}
        producing = child.outputs.get(ref.column)
        if producing is None:
            if child.star:
                self._trace_star(child, ref.column, chain + [hop], next_seen, terminals)
            else:
                terminals.add(_Terminal("", ref.column, "unresolved", tuple(chain + [hop])))
            return
        for inner_ref in producing:
            # Keep the qualifier when the child output names its source
            # relation; dropping it would also defeat the recursion guard.
            self._trace(child, inner_ref, chain + [hop], next_seen, terminals)

    def _trace_star(self, scope: Scope, column: str,
                    chain: list[LineageHop], seen: set[tuple[int, str]],
                    terminals: set[_Terminal]) -> None:
        for rel in scope.rels:
            if rel.kind == "table":
                terminals.add(_Terminal(
                    rel.physical or rel.name, column, "physical",
                    tuple(chain + [LineageHop(rel.physical or rel.name, column, "physical")]),
                ))
            elif rel.scope is not None and (column in rel.scope.outputs or rel.scope.star):
                self._trace(rel.scope, Ref(None, column), chain, seen, terminals)

    def _find_rels(self, scope: Scope, ref: Ref) -> list[Rel]:
        if ref.qualifier is not None:
            for rel in scope.rels:
                if rel.alias == ref.qualifier or rel.name == ref.qualifier:
                    return [rel]
            cte_scope = scope.ctes.get(ref.qualifier)
            if cte_scope is not None:
                return [Rel(kind="cte", alias=ref.qualifier, name=ref.qualifier,
                            scope=cte_scope)]
            return []
        candidates = [rel for rel in scope.rels if self._rel_exposes(rel, ref.column)]
        return candidates

    def _rel_exposes(self, rel: Rel, column: str) -> bool:
        if rel.kind == "table":
            return True
        if rel.scope is None:
            return False
        # Qualified references are already bound to this relation; only bare
        # column names need an output/star check to disambiguate owners.
        return column in rel.scope.outputs or rel.scope.star

    def _physical_of(self, rel: Rel) -> tuple[str, ...]:
        if rel.kind == "table":
            return sort_unique([rel.physical or rel.name])
        if rel.scope is not None:
            return self._scope_physical(rel.scope)
        return ()

    def _scope_physical(self, scope: Scope) -> tuple[str, ...]:
        found: set[str] = set()

        def walk(node: Scope, seen: set[int]) -> None:
            if id(node) in seen:
                return
            seen.add(id(node))
            for rel in node.rels:
                if rel.kind == "table" and rel.physical:
                    found.add(rel.physical)
                elif rel.scope is not None:
                    walk(rel.scope, seen)

        walk(scope, set())
        return sort_unique(found)

    def _recursive_warning(self, name: str) -> None:
        message = (
            f"recursive CTE '{name}' self reference is not expanded "
            "beyond its anchor branch"
        )
        if message not in self.warnings:
            self.warnings.append(message)


def build_lineage(root: Scope, cte_defs: list[tuple[str, Scope]]) -> tuple[
    tuple[str, ...],
    tuple[RelationNode, ...],
    tuple[RelationNode, ...],
    tuple[ColumnLineage, ...],
    list[str],
    "LineageResolver",
]:
    """Resolve a built graph into the structures stored on ``ParsedQuery``.

    Returns the resolver as well so callers can trace aggregate arguments on
    the same object and keep all cycle/shadow warnings in one place.
    """
    resolver = LineageResolver(root, cte_defs)
    tables = resolver.physical_tables()
    warnings = resolver.shadow_warnings(tables) + resolver.warnings
    return (
        tables,
        resolver.relation_tree(root),
        resolver.cte_tree(),
        resolver.field_lineage(),
        warnings,
        resolver,
    )
