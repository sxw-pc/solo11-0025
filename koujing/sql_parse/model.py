"""Parser-neutral data structures and expression canonicalisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


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
    # Surface spelling kept for reporting only; never compared for semantics.
    filter_expr_surface: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aggregates"] = [agg.to_dict() for agg in self.aggregates]
        # Surface spelling is an internal reporting helper, not part of the
        # stable serialised query shape.
        data.pop("filter_expr_surface", None)
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


def _canonicalise(text: str | None) -> str:
    """Surface canonical form: normalise case, qualifiers and whitespace only.

    This is the historical, deliberately order-preserving form. It keeps the
    written order of boolean terms so that callers can still describe how two
    expressions were spelled after the restricted conjunction pass below.
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


# Constructs whose top-level AND/OR tokens are not plain boolean connectives
# (BETWEEN ... AND ..., CASE ... WHEN ...). Rewriting such an expression would
# risk changing semantics, so the pass leaves that level untouched.
_UNSAFE_BOOL_WORDS = {"between", "case", "when", "then", "else", "end"}
_BOOL_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _scan(text: str):
    """Yield ``(kind, value, start, end)`` outside string literals.

    ``kind`` is ``"word"``, ``"open"`` or ``"close"``. Parenthesis depth is
    tracked by the caller; single-quoted literals are skipped, including
    doubled ``''`` escapes and backslash escapes.
    """
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length:
                current = text[index]
                if current == "\\" and index + 1 < length:
                    index += 2
                    continue
                if current == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue
        if char == "(":
            yield "open", char, index, index + 1
        elif char == ")":
            yield "close", char, index, index + 1
        elif char.isalpha() or char == "_":
            match = _BOOL_WORD_RE.match(text, index)
            if match:
                yield "word", match.group(0).lower(), match.start(), match.end()
                index = match.end()
                continue
        index += 1


def _split_bool_keyword(text: str, keyword: str) -> list[str]:
    """Split on a top-level boolean keyword, never inside parentheses/quotes."""
    parts: list[str] = []
    depth = 0
    start = 0
    for kind, value, begin, end in _scan(text):
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth = max(0, depth - 1)
        elif depth == 0 and value == keyword:
            parts.append(text[start:begin].strip())
            start = end
    if not parts:
        return [text.strip()]
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _top_level_words(text: str) -> set[str]:
    depth = 0
    words: set[str] = set()
    for kind, value, _begin, _end in _scan(text):
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth = max(0, depth - 1)
        elif depth == 0:
            words.add(value)
    return words


def _unwrap_outer_parens(text: str) -> str | None:
    """Return the inside of ``text`` when one parenthesis pair wraps it all."""
    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return None
    depth = 0
    for kind, _value, begin, _end in _scan(text):
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
            if depth == 0 and begin != len(text) - 1:
                return None
    return text[1:-1].strip() if depth == 0 else None


def normalise_conjunction(text: str) -> str:
    """Restricted, proof-safe normalisation of boolean conjunctions.

    Only three rewrites are attempted, and only when the expression is a plain
    boolean chain that can be shown equivalent:

    * AND-connected terms are made order-independent (sorted canonically);
    * duplicate AND conjuncts are removed;
    * redundant parentheses around a whole expression are dropped.

    OR chains and comparison chains are never reordered. Anything containing a
    construct whose ``AND``/``OR`` tokens may not be plain connectives
    (``BETWEEN ... AND``, ``CASE ... WHEN``) is returned unchanged. When
    equivalence cannot be proven, the original canonical text is preserved.
    """
    return _normalise_bool(text.strip())


def _has_quoted_identifier(text: str) -> bool:
    """True when a double quote or backtick appears outside a single quote.

    The boolean lexer only understands single-quoted literals; a quoted
    identifier could contain ``and``/``or`` and be mis-split, so such text is
    left untouched.
    """
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length:
                current = text[index]
                if current == "\\" and index + 1 < length:
                    index += 2
                    continue
                if current == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue
        if char in {'"', "`"}:
            return True
        index += 1
    return False


def _collect_conjuncts(text: str) -> list[str]:
    """Flatten an AND-only expression into individual conjuncts.

    Nested AND groups are associative, so ``(a AND b) AND c`` and
    ``a AND b AND c`` may be compared position by position. A parenthesised
    group that contains a top-level OR (or an unsafe construct) stays a single
    conjunct and is never split.
    """
    text = text.strip()
    inner = _unwrap_outer_parens(text)
    if inner is not None and inner:
        if _has_quoted_identifier(inner) or (_top_level_words(inner) & _UNSAFE_BOOL_WORDS):
            return [text]
        if len(_split_bool_keyword(inner, "or")) > 1:
            return [text]
        text = inner
    if _has_quoted_identifier(text) or (_top_level_words(text) & _UNSAFE_BOOL_WORDS):
        return [text]
    parts = _split_bool_keyword(text, "and")
    if len(parts) == 1:
        return [text]
    collected: list[str] = []
    for part in parts:
        collected.extend(_collect_conjuncts(part))
    return collected


def _normalise_bool(text: str) -> str:
    text = text.strip()
    if not text:
        return text

    # Drop redundant parentheses that wrap the whole expression. Track the
    # original text so bail-outs can restore it verbatim instead of silently
    # discarding the parentheses we just removed.
    surface = text
    while True:
        inner = _unwrap_outer_parens(text)
        if inner is None or not inner:
            break
        text = inner

    if _has_quoted_identifier(text) or (_top_level_words(text) & _UNSAFE_BOOL_WORDS):
        # Quoted identifiers are opaque to this lexer; BETWEEN ... AND / CASE
        # ... WHEN are not plain connectives. Keep this level exactly as written.
        return surface

    or_terms = _split_bool_keyword(text, "or")
    if len(or_terms) > 1:
        # OR operands keep their written order; only operands are normalised.
        # A reordered OR chain therefore never compares equal.
        return " or ".join(_normalise_bool(term) for term in or_terms)

    conjuncts = _collect_conjuncts(text)
    if len(conjuncts) > 1:
        normalised = [_normalise_bool(term) for term in conjuncts]
        unique = sorted(set(normalised))
        if len(unique) == 1:
            return unique[0]
        return " and ".join(_wrap_or_term(term) for term in unique)
    return text


def _wrap_or_term(term: str) -> str:
    """Parenthesise a conjunct that itself contains a top-level OR."""
    if len(_split_bool_keyword(term, "or")) > 1:
        return f"({term})"
    return term


def canonical_expr_surface(text: str | None) -> str:
    """Canonical form without conjunction reordering.

    Used only to describe how an expression was written; semantic comparison
    always uses :func:`canonical_expr`.
    """
    return _canonicalise(text)


def canonical_expr(text: str | None) -> str:
    """Canonical, conservative comparison form for filters and expressions.

    Case is folded outside single-quoted string literals, table qualifiers are
    removed, whitespace is collapsed and operator spacing is normalised. String
    literal contents are preserved verbatim. On top of that, only the restricted
    conjunction rewrites in :func:`normalise_conjunction` are applied: AND terms
    are order-independent and de-duplicated, redundant whole-expression
    parentheses are dropped. OR chains and comparison chains are never
    reordered, and expressions whose equivalence cannot be proven are kept
    verbatim.
    """
    return normalise_conjunction(_canonicalise(text))


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
