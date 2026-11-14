"""Bounded extraction of two-table join relationships from a FROM clause.

This module deliberately understands one narrow syntactic shape: a single
level FROM clause joining exactly two physical tables with INNER JOIN or
LEFT JOIN and an ON predicate that is a pure conjunction of equalities
between qualified columns of the two tables.  Everything outside that shape
is reported as *not decidable* with an explicit reason rather than guessed
at.  The extractors never execute SQL and never infer runtime cardinality.

Both parser backends feed their FROM text through :func:`extract_joins` so
the alias-to-physical-table binding and the qualified join endpoints are
represented identically regardless of which parser produced the query.
"""

from __future__ import annotations

import re

from .common import clean_identifier, split_top_level, strip_string_literals
from .model import ColumnRef, JoinEquality, JoinSpec

# Reasons are stable machine-readable codes; human-readable wording lives in
# the relationship checker so the parsers stay vocabulary-neutral.
REASON_SUBQUERY = "subquery_in_from"
REASON_NOT_TWO_TABLES = "not_exactly_two_tables"
REASON_UNSUPPORTED_JOIN_TYPE = "unsupported_join_type"
REASON_CROSS_JOIN = "comma_or_cross_join"
REASON_OR_PREDICATE = "or_in_join_predicate"
REASON_NON_EQUALITY = "non_equality_conjunct"
REASON_UNQUALIFIED_COLUMN = "unqualified_column_in_on"
REASON_UNKNOWN_ALIAS = "column_alias_not_bound"
REASON_NON_COLUMN_ENDPOINT = "join_endpoint_is_not_a_column"
REASON_MULTI_JOIN = "more_than_one_join"
REASON_NO_ON_CLAUSE = "missing_on_clause"

_ANY_JOIN_RE = re.compile(
    r"\b(natural\s+(?:left\s+outer\s+join|left\s+join|inner\s+join|join)|"
    r"cross\s+join|inner\s+join|"
    r"left\s+outer\s+join|left\s+join|right\s+outer\s+join|right\s+join|"
    r"full\s+outer\s+join|full\s+join|join)\b",
    re.I | re.S,
)
_QUALIFIED_COLUMN_RE = re.compile(
    r"^[`\"\[\]]?([A-Za-z_]\w*)[`\"\[\]]?\s*\.\s*[`\"\[\]]?([A-Za-z_]\w*)[`\"\[\]]?$"
)
_TABLE_REF_RE = re.compile(r"^[`\"\[\]\w.$]+$")
_SIMPLE_IDENT_RE = re.compile(r"^[`\"\[\]]?[A-Za-z_]\w*[`\"\[\]]?$")


def _split_top_and(text: str) -> list[str]:
    """Split a predicate at top-level AND keywords (parens/quotes aware)."""
    parts: list[str] = []
    depth = 0
    start = 0
    tokens = re.finditer(r"\(|\)|\bAND\b|\bOR\b", text, flags=re.I)
    for token in tokens:
        if token.group(0) == "(":
            depth += 1
        elif token.group(0) == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and token.group(0).lower() == "and":
            parts.append(text[start:token.start()].strip())
            start = token.end()
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _unwrap_group(text: str) -> str:
    """Remove one outer parenthesis pair when it spans the whole fragment."""
    candidate = text.strip()
    if not (candidate.startswith("(") and candidate.endswith(")")):
        return candidate
    depth = 0
    for index, char in enumerate(candidate):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(candidate) - 1:
                return candidate
    return candidate[1:-1].strip() if depth == 0 else candidate


def _contains_top_level_or(text: str) -> bool:
    # A conjunct wrapped in parentheses is itself a group; an OR inside it is
    # still a disjunctive join predicate and must be rejected.
    candidate = text.strip()
    while True:
        unwrapped = _unwrap_group(candidate)
        if unwrapped == candidate:
            break
        candidate = unwrapped
    depth = 0
    for token in re.finditer(r"\(|\)|\bOR\b", candidate, flags=re.I):
        if token.group(0) == "(":
            depth += 1
        elif token.group(0) == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            return True
    return False


def _undecidable(join_type: str, left_alias: str, left_table: str,
                 right_alias: str, right_table: str,
                 reasons: list[str]) -> JoinSpec:
    unique = tuple(dict.fromkeys(reasons))
    return JoinSpec(
        join_type=join_type,
        left_alias=left_alias,
        left_table=left_table,
        right_alias=right_alias,
        right_table=right_table,
        equalities=(),
        decidable=False,
        undecidable_reasons=unique,
    )


def _normalise_join_type(raw: str) -> str:
    folded = re.sub(r"\s+", " ", raw.strip().lower())
    if folded.startswith("natural"):
        # Natural joins hide their equality columns, so they are never
        # checkable in the bounded subset regardless of the outer side.
        return "natural"
    if folded == "join":
        return "inner"
    if folded in {"left join", "left outer join"}:
        return "left"
    return folded.replace(" join", "").replace(" outer", "")


def _table_reference(token: str) -> tuple[str, str] | None:
    """Return ``(alias, physical_table)`` for a simple table reference."""
    token = token.strip()
    if not token or not _TABLE_REF_RE.fullmatch(token):
        return None
    pieces = [clean_identifier(piece) for piece in token.split(".")]
    physical = pieces[-1]
    return physical, physical


def _split_segments(from_clause: str) -> list[str]:
    """Split a FROM clause at top-level join keywords, keeping the keyword."""
    cleaned = strip_string_literals(from_clause)
    segments: list[str] = []
    cursor = 0
    for match in _ANY_JOIN_RE.finditer(cleaned):
        head = cleaned[cursor:match.start()].strip()
        if head:
            segments.append(head)
        segments.append(match.group(1).lower())
        cursor = match.end()
    tail = cleaned[cursor:].strip()
    if tail:
        segments.append(tail)
    return segments


def _parse_side(side_text: str) -> tuple[str, str] | None:
    """Parse one side of a join: ``physical_table [AS] alias`` or bare table."""
    tokens = side_text.strip().split()
    tokens = [token for token in tokens if token.lower() != "as"]
    if len(tokens) == 1:
        return _table_reference(tokens[0])
    if len(tokens) == 2:
        base = _table_reference(tokens[0])
        alias_match = _SIMPLE_IDENT_RE.fullmatch(tokens[1].strip())
        if base is None or alias_match is None:
            return None
        return alias_match.group(0).strip("`\"[]").lower(), base[1]
    return None


def _column_ref(text: str, bindings: dict[str, str]) -> tuple[ColumnRef | None, str | None]:
    match = _QUALIFIED_COLUMN_RE.fullmatch(text.strip())
    if match is None:
        if re.fullmatch(r"[`\"\[\]]?[A-Za-z_]\w*[`\"\[\]]?", text.strip() or ""):
            return None, REASON_UNQUALIFIED_COLUMN
        return None, REASON_NON_COLUMN_ENDPOINT
    alias = match.group(1).lower()
    column = match.group(2).lower()
    table = bindings.get(alias)
    if table is None:
        return None, REASON_UNKNOWN_ALIAS
    return ColumnRef(alias=alias, table=table, column=column), None


def _parse_on(on_text: str, bindings: dict[str, str],
              left_alias: str, right_alias: str) -> tuple[tuple[JoinEquality, ...], list[str]]:
    reasons: list[str] = []
    equalities: list[JoinEquality] = []
    conjuncts: list[str] = []
    for piece in _split_top_and(on_text):
        unwrapped = _unwrap_group(piece)
        if unwrapped != piece and re.search(r"\band\b", unwrapped, flags=re.I):
            conjuncts.extend(_split_top_and(unwrapped))
        else:
            conjuncts.append(piece)
    for conjunct in conjuncts:
        while True:
            unwrapped = _unwrap_group(conjunct)
            if unwrapped == conjunct:
                break
            conjunct = unwrapped
        folded = re.sub(r"\s+", " ", conjunct.strip().lower())
        if _contains_top_level_or(conjunct):
            reasons.append(REASON_OR_PREDICATE)
            continue
        parts = re.split(r"\s*=\s*", conjunct.strip(), maxsplit=1)
        if len(parts) != 2 or re.search(r"<>|!=|<=|>=|<|>|\b(is|between|like|in)\b", folded):
            reasons.append(REASON_NON_EQUALITY)
            continue
        left_ref, left_error = _column_ref(parts[0], bindings)
        right_ref, right_error = _column_ref(parts[1], bindings)
        if left_error or right_error:
            reasons.append(left_error or right_error)
            continue
        # Re-orient so the declared-left table's column is always first; the
        # textual order of an equality must not change the relationship.
        if left_ref.alias == right_alias and right_ref.alias == left_alias:
            left_ref, right_ref = right_ref, left_ref
        elif left_ref.alias != left_alias or right_ref.alias != right_alias:
            reasons.append(REASON_NON_COLUMN_ENDPOINT)
            continue
        equalities.append(JoinEquality(left=left_ref, right=right_ref))
    return tuple(equalities), reasons


def extract_joins(from_clause: str) -> tuple[JoinSpec, ...]:
    """Extract the bounded join shape from a FROM-clause fragment.

    Returns an empty tuple for single-table FROM clauses and one
    :class:`JoinSpec` (decidable or explicitly undecidable) otherwise.
    """
    if not from_clause or not from_clause.strip():
        return ()
    bare = strip_string_literals(from_clause)
    subquery = (
        re.match(r"\s*\(", bare)
        or re.search(r"\bjoin\s*\(", bare, flags=re.I)
        or re.search(r"\(\s*select\b", bare, flags=re.I)
    )
    if subquery:
        return (_undecidable("", "", "", "", "", [REASON_SUBQUERY]),)

    # A top-level comma is an implicit (cross) join, which is out of scope.
    if len(split_top_level(bare)) > 1:
        return (_undecidable("", "", "", "", "", [REASON_CROSS_JOIN]),)

    segments = _split_segments(bare)
    if not segments:
        return ()

    join_keywords = [segment for segment in segments if re.fullmatch(r"[a-z ]+", segment)
                     and segment.endswith("join")]
    if not join_keywords:
        return ()
    if len(join_keywords) != 1 or len(segments) != 3:
        return (_undecidable("", "", "", "", "", [REASON_MULTI_JOIN]),)

    raw_type, left_text, right_text = segments[1], segments[0], segments[2]
    if raw_type == "cross join":
        return (_undecidable("", "", "", "", "", [REASON_CROSS_JOIN]),)
    join_type = _normalise_join_type(raw_type)
    if join_type not in {"inner", "left"}:
        return (_undecidable(join_type, "", "", "", "",
                             [REASON_UNSUPPORTED_JOIN_TYPE]),)

    on_match = re.split(r"\bon\b", right_text, maxsplit=1, flags=re.I)
    if len(on_match) != 2:
        return (_undecidable(join_type, "", "", "", "",
                             [REASON_NO_ON_CLAUSE]),)
    left_side = _parse_side(left_text)
    right_side = _parse_side(on_match[0])
    if left_side is None or right_side is None:
        return (_undecidable(join_type, "", "", "", "", [REASON_NOT_TWO_TABLES]),)

    left_alias, left_table = left_side
    right_alias, right_table = right_side
    bindings = {left_alias: left_table, right_alias: right_table}
    equalities, reasons = _parse_on(on_match[1], bindings, left_alias, right_alias)
    if reasons or not equalities:
        if not equalities and not reasons:
            reasons.append(REASON_NON_EQUALITY)
        return (_undecidable(join_type, left_alias, left_table,
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
