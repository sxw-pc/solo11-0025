"""A deliberately small, standard-library-only YAML subset parser.

It supports the shapes used by metric dictionaries: nested mappings, block
sequences, inline lists/maps and scalar strings/numbers/booleans/null. It is
not a general YAML implementation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class YamlLiteError(ValueError):
    """Raised for malformed input in the supported YAML subset."""


@dataclass(frozen=True)
class _Line:
    indent: int
    text: str
    number: int


def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _tokenize(text: str) -> list[_Line]:
    tokens: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        expanded = raw.replace("\t", "    ")
        cleaned = _strip_comment(expanded).rstrip()
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        content = cleaned[indent:]
        if content in {"---", "..."}:
            continue
        tokens.append(_Line(indent, content, number))
    return tokens


def _split_top(text: str, separator: str = ",") -> list[str]:
    pieces: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == separator and depth == 0:
            pieces.append(text[start:index].strip())
            start = index + 1
    pieces.append(text[start:].strip())
    return pieces


def _split_key(text: str) -> tuple[str, str] | None:
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == ":" and depth == 0:
            key = text[:index].strip()
            value = text[index + 1:].strip()
            if not key:
                return None
            return _unquote_key(key), value
    return None


def _unquote_key(key: str) -> str:
    value = _parse_scalar(key)
    return value if isinstance(value, str) else str(value)


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [] if not inner else [_parse_scalar(item) for item in _split_top(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        result: dict[str, Any] = {}
        if inner:
            for item in _split_top(inner):
                pair = _split_key(item)
                if pair is None:
                    raise YamlLiteError(f"invalid inline mapping item: {item!r}")
                key, value = pair
                result[key] = _parse_scalar(value)
        return result
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[1:-1].replace('\\"', '"')
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        try:
            return int(text)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", text):
        try:
            return float(text)
        except ValueError:
            pass
    return text


def _is_sequence(line: _Line) -> bool:
    return line.text == "-" or line.text.startswith("- ")


def _block_scalar(lines: list[_Line], index: int, parent_indent: int, folded: bool) -> tuple[str, int]:
    parts: list[str] = []
    body_indent: int | None = None
    while index < len(lines) and lines[index].indent > parent_indent:
        line = lines[index]
        if body_indent is None:
            body_indent = line.indent
        parts.append(" " * max(0, line.indent - body_indent) + line.text)
        index += 1
    joiner = " " if folded else "\n"
    return joiner.join(parts), index


def _parse_block(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    if _is_sequence(lines[index]):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_sequence(lines: list[_Line], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines) and lines[index].indent == indent and _is_sequence(lines[index]):
        dash_line = lines[index]
        rest = dash_line.text[1:].strip()
        index += 1
        if rest and _split_key(rest) is None:
            items.append(_parse_scalar(rest))
            continue
        sublines: list[_Line] = []
        if rest:
            sublines.append(_Line(indent + 2, rest, dash_line.number))
        while index < len(lines) and lines[index].indent > indent:
            sublines.append(lines[index])
            index += 1
        if not sublines:
            items.append(None)
        else:
            value, _ = _parse_block(sublines, 0, sublines[0].indent)
            items.append(value)
    return items, index


def _parse_mapping(lines: list[_Line], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent and not _is_sequence(lines[index]):
        line = lines[index]
        pair = _split_key(line.text)
        if pair is None:
            raise YamlLiteError(f"expected 'key: value' on line {line.number}")
        key, raw = pair
        if key in result:
            raise YamlLiteError(f"duplicate key {key!r} on line {line.number}")
        index += 1
        if raw in {"|", ">"}:
            value, index = _block_scalar(lines, index, indent, folded=raw == ">")
            result[key] = value
            continue
        if raw == "":
            if index < len(lines) and lines[index].indent > indent:
                value, index = _parse_block(lines, index, lines[index].indent)
            else:
                value = None
            result[key] = value
            continue
        result[key] = _parse_scalar(raw)
        if index < len(lines) and lines[index].indent > indent:
            raise YamlLiteError(f"unexpected indentation after {key!r} on line {lines[index].number}")
    return result, index


def loads(text: str) -> Any:
    """Parse a supported YAML document from *text*."""
    lines = _tokenize(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise YamlLiteError(f"could not parse line {lines[index].number}")
    return value


def load_file(path: Any) -> Any:
    from pathlib import Path

    return loads(Path(path).read_text(encoding="utf-8"))
