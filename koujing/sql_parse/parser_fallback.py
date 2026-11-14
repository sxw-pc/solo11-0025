"""Standard-library SQL subset parser used when sqlglot is unavailable."""

from __future__ import annotations

import re

from .common import extract_identifiers, line_range, split_top_level, strip_comments, top_level_words
from .model import Aggregate, ParsedQuery, canonical_expr, detect_granularity, sort_unique
from .ratio import RATIO_FUNCTION, parse_ratio_expression, ratio_fields, split_projection_alias

AGGREGATE_RE = re.compile(
    r"^(count|sum|avg|max|min)\s*\((.*)\)\s*(?:(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?\s*$",
    re.I | re.S,
)
DISTINCT_RE = re.compile(r"^distinct\s+(.*)$", re.I | re.S)
TABLE_RE = re.compile(r"\b(?:from|join)\s+([^\s,;]+)", re.I)
UNSUPPORTED = {"qualify", "window", "emit", "match_recognize", "pivot", "unpivot", "connect"}


def _clause_starts(words: list) -> dict[str, int]:
    starts: dict[str, int] = {}
    names = {"from", "where", "group", "having", "order", "limit", "union"}
    for index, word in enumerate(words):
        if word.value in names and word.value not in starts:
            starts[word.value] = word.start
    return starts


def _section_end(words: list, start_index: int, allowed: set[str]) -> int:
    for word in words[start_index:]:
        if word.value in allowed:
            return word.start
    return -1


def _normalise_table(raw: str) -> str:
    value = raw.strip().strip("`\"[]")
    value = value.rsplit(".", 1)[-1].strip("`\"[]")
    return value.lower()


def _tables(from_clause: str) -> tuple[str, ...]:
    if not from_clause.strip():
        return ()
    if re.search(r"\bfrom\s*\(", from_clause, flags=re.I):
        return ()
    found = []
    for match in TABLE_RE.finditer("from " + from_clause):
        raw = match.group(1)
        if raw.startswith("(") or not re.fullmatch(r"[`\"\[\]\w.$]+", raw):
            continue
        found.append(_normalise_table(raw))
    for segment in split_top_level(from_clause)[1:]:
        raw = segment.split(None, 1)[0] if segment.strip() else ""
        if raw and not raw.startswith("(") and re.fullmatch(r"[`\"\[\]\w.$]+", raw):
            found.append(_normalise_table(raw))
    return sort_unique(found)


def _balanced(text: str) -> bool:
    depth = 0
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _aggregate_from_item(item: str) -> Aggregate | None:
    match = AGGREGATE_RE.match(item.strip())
    if not match:
        return None
    function = match.group(1).lower()
    argument = match.group(2).strip()
    alias = (match.group(3) or "").lower()
    if not _balanced(argument):
        # The greedy ``(.*)`` regex swallowed ``SUM(x) / SUM(y)`` as if the
        # outer ``SUM`` covered everything. Leave it to the ratio extractor.
        return None
    if function == "avg" and _has_top_level_division(argument):
        # ``AVG(x / y)`` is an average ratio. Let ``_ratio_from_item`` see the
        # whole projection so the composite structure is preserved instead of
        # collapsing it into a legacy single-AVG aggregate.
        return None
    distinct = DISTINCT_RE.match(argument)
    if function == "count" and distinct:
        function = "count_distinct"
        argument = distinct.group(1).strip()
    return Aggregate(
        function=function,
        expression=canonical_expr(argument),
        alias=alias,
        fields=extract_identifiers(argument),
    )


def _has_top_level_division(text: str) -> bool:
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "/" and depth == 0:
            return True
        index += 1
    return False


def _ratio_from_item(item: str) -> Aggregate | None:
    """Catch ratio projections whose outer form is not a bare aggregate."""
    core, alias = split_projection_alias(item)
    ratio = parse_ratio_expression(core)
    if ratio is None:
        return None
    return Aggregate(
        function=RATIO_FUNCTION,
        expression=canonical_expr(core),
        alias=alias,
        fields=ratio_fields(ratio),
        ratio=ratio,
    )


def _strip_implicit_alias(item: str) -> str:
    match = re.match(r"^(.*\))\s+[A-Za-z_][A-Za-z0-9_]*\s*$", item.strip(), re.S)
    return match.group(1) if match else item


def _select_parts(select_clause: str) -> tuple[list[Aggregate], tuple[str, ...]]:
    aggregates: list[Aggregate] = []
    fields: list[str] = []
    for item in split_top_level(select_clause):
        aggregate = _aggregate_from_item(item)
        if aggregate is None:
            aggregate = _ratio_from_item(item)
        if aggregate is not None:
            aggregates.append(aggregate)
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
    if re.search(r"\bfrom\s*\(", clean, flags=re.I):
        warnings.append("subquery in FROM is only partially supported")

    tables = _tables(sections.get("from", ""))
    aggregates, select_fields = _select_parts(sections.get("select", ""))
    where_expr = canonical_expr(sections.get("where", ""))
    group_exprs = tuple(
        sorted({canonical_expr(item) for item in split_top_level(sections.get("group", "")) if item})
    )
    fields = list(select_fields)
    fields.extend(extract_identifiers(sections.get("where", "")))
    fields.extend(extract_identifiers(sections.get("group", "")))
    if not tables:
        warnings.append("no base table recognised")
    if not aggregates:
        warnings.append("no direct aggregate projection recognised")
    for aggregate in aggregates:
        if not aggregate.alias:
            warnings.append(f"aggregate {aggregate.function} has no alias; no metric candidate")
    return ParsedQuery(
        source=source,
        tables=tables,
        select_fields=sort_unique(fields),
        aggregates=tuple(aggregates),
        filter_expr=where_expr,
        group_by=group_exprs,
        granularity=detect_granularity(group_exprs),
        warnings=tuple(dict.fromkeys(warnings)),
        line_start=start_line,
        line_end=end_line,
    )
