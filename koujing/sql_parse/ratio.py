"""Shared, text-level extraction of derived ratio projections.

Both parser implementations must expose the *same* composite structure for the
same SQL on their common subset. Rather than duplicating AST and regex logic,
each parser hands the canonical projection text to :func:`parse_ratio_expression`
and wraps the returned :class:`~koujing.sql_parse.model.RatioSpec`.

The extraction is intentionally conservative: a derived alias is never dropped
because one operand is outside the supported subset. Instead the operand is
kept verbatim, marked unsupported, and the comparison layer reports
``undetermined`` with the concrete reason.
"""

from __future__ import annotations

import re

from .common import extract_identifiers, split_top_level
from .model import RatioPart, RatioSpec, canonical_expr

RATIO_FUNCTION = "ratio"

_AGGREGATE_FUNCTIONS = {"sum", "count", "avg", "max", "min"}
_COALESCE_FUNCTIONS = {"coalesce", "ifnull"}
# Scalar presentation wrappers we can name but not see through. A ratio hidden
# behind one of these stays a candidate so it becomes ``undetermined`` rather
# than silently disappearing from the report.
_WRAPPER_FUNCTIONS = {"round", "cast", "try_cast", "abs", "greatest", "least"}
_FUNCTION_PREFIX_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*\(")
_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*")
_AS_ALIAS_RE = re.compile(r"^(.*\S)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I | re.S)
_IMPLICIT_ALIAS_RE = re.compile(r"^(.*\))\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.S)
_DISTINCT_RE = re.compile(r"^distinct\s+(.*)$", re.I | re.S)
_ZERO_LITERAL_RE = re.compile(
    r"^[+-]?(?:0+(?:\.0*)?|\.0+)(?:[eE][+-]?\d+)?$"
)


def split_projection_alias(item: str) -> tuple[str, str]:
    """Split ``<expression> AS <alias>`` (or the implicit form)."""
    text = item.strip()
    match = _AS_ALIAS_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).lower()
    match = _IMPLICIT_ALIAS_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).lower()
    return text, ""


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote = ""
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if quote == "'" and char == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            if char == quote:
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
    return -1


def _unwrap_function(text: str) -> tuple[str, str] | None:
    """Return ``(name, arguments)`` when *text* is one whole function call."""
    match = _FUNCTION_PREFIX_RE.match(text)
    if not match:
        return None
    open_index = text.find("(", match.end() - 1)
    close_index = _matching_paren(text, open_index)
    if close_index != len(text) - 1:
        return None
    return match.group(1).lower(), text[open_index + 1:close_index].strip()


def _top_level_divisions(text: str) -> list[int]:
    positions: list[int] = []
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == "'" and char == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "/" and depth == 0:
            positions.append(index)
        index += 1
    return positions


def _top_level_function_names(text: str) -> list[str]:
    names: list[str] = []
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == "'" and char == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            match = _WORD_RE.match(text, index)
            if not match:
                index += 1
                continue
            name = match.group(0)
            after = match.end()
            while after < len(text) and text[after].isspace():
                after += 1
            if after < len(text) and text[after] == "(":
                names.append(name)
            index = match.end()
            continue
        index += 1
    return names


def _is_zero_literal(text: str) -> bool:
    value = text.strip()
    if not _ZERO_LITERAL_RE.match(value):
        return False
    try:
        return float(value) == 0.0
    except ValueError:
        return False


_AGGREGATE_CALL_RE = re.compile(r"\b(sum|count|avg|max|min)\s*\(", re.I)


def _contains_aggregate(text: str) -> bool:
    return bool(_AGGREGATE_CALL_RE.search(text))


def _part_fields(expression: str) -> tuple[str, ...]:
    return extract_identifiers(expression)


def _parse_aggregate_part(text: str) -> RatioPart | None:
    unwrapped = _unwrap_function(text)
    if unwrapped is None:
        return None
    name, argument = unwrapped
    if name == "count":
        distinct = _DISTINCT_RE.match(argument)
        if distinct:
            argument = distinct.group(1).strip()
            function = "count_distinct"
        else:
            function = "count"
    elif name in _AGGREGATE_FUNCTIONS:
        function = name
    else:
        return None
    if not argument:
        return None
    return RatioPart(
        aggregation=function,
        expression=canonical_expr(argument),
        fields=_part_fields(argument),
    )


def _parse_ratio_part(text: str, side: str) -> tuple[RatioPart, str]:
    """Return ``(part, reason)``; *reason* is empty for a supported part."""
    stripped = text.strip()
    while stripped.startswith("(") and _matching_paren(stripped, 0) == len(stripped) - 1:
        inner = stripped[1:-1].strip()
        if re.search(r"\bselect\b", inner, flags=re.I):
            break
        stripped = inner
    if re.search(r"\bselect\b", stripped, flags=re.I) or re.search(r"\bfrom\b", stripped, flags=re.I):
        reason = f"{side} contains a subquery"
        return RatioPart("", stripped, _part_fields(stripped)), reason
    aggregate = _parse_aggregate_part(stripped)
    if aggregate is not None:
        return aggregate, ""
    unwrapped = _unwrap_function(stripped)
    if unwrapped is not None:
        name = unwrapped[0]
        reason = f"{side} uses unsupported function {name!r}"
        return RatioPart("", stripped, _part_fields(stripped)), reason
    functions = _top_level_function_names(stripped)
    if functions:
        unknown = sorted(set(functions) - _AGGREGATE_FUNCTIONS)
        detail = ", ".join(repr(name) for name in unknown) or ", ".join(sorted(set(functions)))
        reason = f"{side} uses unsupported function {detail}"
        return RatioPart("", stripped, _part_fields(stripped)), reason
    return RatioPart("row", stripped, _part_fields(stripped)), ""


def _split_division(text: str) -> tuple[str, str] | None:
    positions = _top_level_divisions(text)
    if len(positions) != 1:
        return None
    position = positions[0]
    return text[:position].strip(), text[position + 1:].strip()


def _detect_zero_guard(denominator: str) -> tuple[str, str]:
    """Return ``(policy, real_denominator)`` for the supported guard forms."""
    unwrapped = _unwrap_function(denominator)
    if unwrapped is not None:
        name, argument = unwrapped
        arguments = split_top_level(argument)
        if name == "nullif" and len(arguments) == 2 and _is_zero_literal(arguments[1]):
            return "nullif", arguments[0].strip()
        if name in _COALESCE_FUNCTIONS and arguments:
            inner = _unwrap_function(arguments[0].strip())
            if inner is not None and inner[0] == "nullif":
                inner_args = split_top_level(inner[1])
                if len(inner_args) == 2 and _is_zero_literal(inner_args[1]):
                    return "coalesce_zero", inner_args[0].strip()
    return "unprotected", denominator


def _parse_population_ratio(expression: str, coalesced: bool) -> RatioSpec | None:
    split = _split_division(expression)
    if split is None:
        return None
    numerator_text, denominator_text = split
    numerator, numerator_reason = _parse_ratio_part(numerator_text, "numerator")
    policy, denominator_body = _detect_zero_guard(denominator_text)
    if coalesced:
        policy = "coalesce_zero"
    denominator, denominator_reason = _parse_ratio_part(denominator_body, "denominator")
    reasons: list[str] = []
    if numerator_reason:
        reasons.append(numerator_reason)
    if denominator_reason:
        reasons.append(denominator_reason)
    return RatioSpec(
        kind="population_ratio",
        numerator=numerator,
        denominator=denominator,
        zero_division=policy,
        expression=expression,
        unsupported=tuple(reasons),
    )


def _parse_average_ratio(expression: str, coalesced: bool) -> RatioSpec | None:
    unwrapped = _unwrap_function(expression)
    if unwrapped is None or unwrapped[0] != "avg":
        return None
    inner = unwrapped[1]
    split = _split_division(inner)
    if split is None:
        return None
    numerator_text, denominator_text = split
    numerator, numerator_reason = _parse_ratio_part(numerator_text, "numerator")
    policy, denominator_body = _detect_zero_guard(denominator_text)
    if coalesced:
        policy = "coalesce_zero"
    denominator, denominator_reason = _parse_ratio_part(denominator_body, "denominator")
    reasons: list[str] = []
    if numerator_reason:
        reasons.append(numerator_reason)
    if denominator_reason:
        reasons.append(denominator_reason)
    return RatioSpec(
        kind="average_ratio",
        numerator=numerator,
        denominator=denominator,
        zero_division=policy,
        expression=expression,
        unsupported=tuple(reasons),
    )


def _parse_multiple_division(expression: str) -> RatioSpec:
    positions = _top_level_divisions(expression)
    first = positions[0]
    numerator, numerator_reason = _parse_ratio_part(expression[:first].strip(), "numerator")
    denominator = RatioPart(
        aggregation="",
        expression=expression[first + 1:].strip(),
        fields=_part_fields(expression[first + 1:]),
    )
    reason = "expression has multiple top-level divisions"
    reasons = [reason]
    if numerator_reason:
        reasons.insert(0, numerator_reason)
    return RatioSpec(
        kind="population_ratio",
        numerator=numerator,
        denominator=denominator,
        zero_division="unprotected",
        expression=expression,
        unsupported=tuple(reasons),
    )


def _division_operands(text: str) -> tuple[str, str] | None:
    """Find one division anywhere inside *text*, ignoring parentheses depth.

    Used only to *recognise* a ratio hidden behind an unknown wrapper; the
    resulting spec is always marked unsupported, so a false positive degrades
    to ``undetermined`` rather than to a wrong comparison.
    """
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == "'" and char == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "/" and depth == 0:
            return text[:index].strip(), text[index + 1:].strip()
        index += 1
    return None


def _parse_wrapped_ratio(expression: str) -> RatioSpec | None:
    operands = _division_operands(expression)
    if operands is None:
        # ``ROUND(SUM(x) / SUM(y), 2)`` hides the division one level deeper.
        unwrapped = _unwrap_function(expression)
        if unwrapped is None:
            return None
        operands = _division_operands(unwrapped[1])
    if operands is None:
        return None
    return RatioSpec(
        kind="population_ratio",
        numerator=RatioPart("", operands[0], _part_fields(operands[0])),
        denominator=RatioPart("", operands[1], _part_fields(operands[1])),
        zero_division="unprotected",
        expression=expression,
        unsupported=("ratio is wrapped in an unsupported expression",),
    )


def parse_ratio_expression(text: str) -> RatioSpec | None:
    """Extract a composite ratio from a projection expression, if any.

    Returns ``None`` when the expression is not a derived ratio at all (a
    plain ``SUM(x)`` or a sum of per-row expressions such as ``SUM(x / y)``
    keep their legacy single-aggregate shape).
    """
    if not text:
        return None
    expression = canonical_expr(text)
    if not expression:
        return None

    coalesced = False
    unwrapped = _unwrap_function(expression)
    if unwrapped is not None and unwrapped[0] in _COALESCE_FUNCTIONS:
        arguments = split_top_level(unwrapped[1])
        if arguments and _has_ratio_shape(arguments[0]):
            coalesced = True
            expression = arguments[0].strip()
            unwrapped = _unwrap_function(expression)

    if unwrapped is not None and unwrapped[0] == "avg" and _has_division(unwrapped[1]):
        return _parse_average_ratio(expression, coalesced)

    positions = _top_level_divisions(expression)
    if positions:
        if len(positions) > 1:
            return _parse_multiple_division(expression)
        return _parse_population_ratio(expression, coalesced)

    if unwrapped is not None and unwrapped[0] in _WRAPPER_FUNCTIONS:
        return _parse_wrapped_ratio(expression)
    return None


def _has_division(text: str) -> bool:
    return bool(_top_level_divisions(text))


def _has_ratio_shape(text: str) -> bool:
    unwrapped = _unwrap_function(text)
    if unwrapped is not None and unwrapped[0] == "avg":
        return _has_division(unwrapped[1])
    return _has_division(text)


def ratio_fields(ratio: RatioSpec) -> tuple[str, ...]:
    return tuple(sorted(set(ratio.numerator.fields) | set(ratio.denominator.fields)))
