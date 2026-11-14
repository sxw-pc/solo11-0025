"""Small lexical helpers shared by both SQL parser implementations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import SQL_KEYWORDS
from .model import canonical_expr

WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Word:
    value: str
    start: int
    end: int


def strip_comments(sql: str) -> str:
    """Remove SQL comments while preserving newlines and string literals."""
    output: list[str] = []
    index = 0
    quote = ""
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            output.append(char)
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline == -1:
                break
            output.append("\n")
            index = newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                output.append(" " * (len(sql) - index))
                break
            chunk = sql[index:end + 2]
            output.append("\n" * chunk.count("\n") or " ")
            index = end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def strip_string_literals(text: str) -> str:
    """Replace single-quoted literal contents with spaces."""
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    output.extend((" ", " "))
                    index += 2
                    continue
                quote = ""
                output.append(" ")
            else:
                output.append(" " if char != "\n" else "\n")
            index += 1
            continue
        if char == "'":
            quote = char
            output.append(" ")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    pieces: list[str] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            pieces.append(text[start:index].strip())
            start = index + 1
        index += 1
    pieces.append(text[start:].strip())
    return [piece for piece in pieces if piece]


def top_level_words(sql: str) -> list[Word]:
    """Return word tokens outside parentheses, quotes and comments."""
    words: list[Word] = []
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    break
                index += 1
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
            match = WORD_RE.match(sql, index)
            if match:
                words.append(Word(match.group(0).lower(), match.start(), match.end()))
                index = match.end()
                continue
        index += 1
    return words


def find_words(words: list[Word], value: str, start_index: int = 0) -> int:
    for index in range(start_index, len(words)):
        if words[index].value == value:
            return index
    return -1


def clean_identifier(raw: str) -> str:
    value = raw.strip().strip("`\"[]")
    if "." in value:
        value = value.rsplit(".", 1)[-1].strip("`\"[]")
    return value.lower()


def extract_identifiers(text: str) -> tuple[str, ...]:
    """Extract likely column references from a SQL expression fragment."""
    cleaned = strip_comments(text)
    # INTERVAL unit words (e.g. the HOUR in INTERVAL '4' HOUR) are temporal
    # units, not column references; mask them before string literals are
    # blanked so both parsers see the same identifier set.
    cleaned = re.sub(
        r"\binterval\s+(?:'(?:''|[^'])*'|\d+)\s*"
        r"(?:second|minute|hour|day|week|month|quarter|year)s?\b",
        " ",
        cleaned,
        flags=re.I,
    )
    # AT TIME ZONE '<zone>' is a conversion clause, not a column reference.
    cleaned = re.sub(r"\bat\s+time\s+zone\b", " ", cleaned, flags=re.I)
    cleaned = strip_string_literals(cleaned)
    cleaned = re.sub(r"\bas\s+[A-Za-z_][A-Za-z0-9_]*", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"'(?:''|[^'])*'", " ", cleaned)
    found: set[str] = set()
    for match in re.finditer(r"(?<![\w.])(?:[A-Za-z_]\w*\s*\.\s*)*[A-Za-z_]\w*", cleaned):
        token = match.group(0)
        if re.search(r"[.`\"\[\]]", token):
            token = re.split(r"\s*\.\s*", token)[-1]
        token = token.strip().strip("`\"[]").lower()
        if not token or token in SQL_KEYWORDS or token.isdigit():
            continue
        tail = cleaned[match.end():match.end() + 24]
        if re.match(r"\s*\(", tail):
            continue
        if token in {"count", "sum", "avg", "max", "min", "round", "cast"}:
            continue
        found.add(token)
    return tuple(sorted(found))


def line_range(sql: str) -> tuple[int, int]:
    searchable = strip_comments(sql)
    select_match = re.search(r"\bselect\b", searchable, flags=re.I)
    if select_match:
        line_start = searchable[:select_match.start()].count("\n") + 1
    else:
        line_start = 1
    non_empty = [index for index, line in enumerate(sql.splitlines(), start=1) if line.strip()]
    line_end = max(non_empty) if non_empty else line_start
    return line_start, max(line_start, line_end)


def extract_filter(where_clause: str) -> str:
    return canonical_expr(where_clause)
