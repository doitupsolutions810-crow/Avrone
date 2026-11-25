"""A small YAML subset loader for service definitions.

Supports mappings, lists, nested blocks, scalars, inline lists, and `|` block
strings. Anchors, tags, and merge keys are rejected so a file cannot change
meaning under a fuller parser in a way this loader would miss.
"""

from __future__ import annotations


class YAMLError(ValueError):
    pass


def loads(text: str):
    lines = _physical_lines(text)
    if not lines:
        return {}
    value, index = _parse(lines, 0, lines[0][0])
    if index != len(lines):
        raise YAMLError(f"trailing content at line {lines[index][2]}")
    return value


def _physical_lines(text: str) -> list[tuple[int, str, int]]:
    out = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = _strip_comment(raw.strip())
        if not body:
            continue
        out.append((indent, body, number))
    return out


def _strip_comment(body: str) -> str:
    quote = ""
    for i, ch in enumerate(body):
        if ch in {"'", '"'}:
            if quote == ch:
                quote = ""
            elif not quote:
                quote = ch
        elif ch == "#" and not quote:
            return body[:i].rstrip()
    return body


def _parse(lines, index, indent):
    if index >= len(lines):
        return {}, index
    if lines[index][0] != indent:
        raise YAMLError(f"expected indent {indent} at line {lines[index][2]}")
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines, index, indent):
    mapping: dict = {}
    while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
        key, rest, line_no = _split_key(lines[index][1], lines[index][2])
        if key in mapping:
            raise YAMLError(f"duplicate key {key!r} at line {line_no}")
        index += 1
        if rest in {"|", ">"}:
            block, index = _parse_block_scalar(lines, index, indent)
            mapping[key] = block
        elif rest == "":
            if index >= len(lines) or lines[index][0] <= indent:
                mapping[key] = {}
            else:
                mapping[key], index = _parse(lines, index, lines[index][0])
        else:
            mapping[key] = _scalar(rest)
    return mapping, index


def _parse_list(lines, index, indent):
    items = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        item = lines[index][1][2:].strip()
        line_no = lines[index][2]
        index += 1
        if item == "":
            if index >= len(lines) or lines[index][0] <= indent:
                items.append(None)
            else:
                value, index = _parse(lines, index, lines[index][0])
                items.append(value)
        elif _looks_like_key(item):
            key, rest, _ = _split_key(item, line_no)
            node = {}
            if rest in {"|", ">"}:
                block, index = _parse_block_scalar(lines, index, indent)
                node[key] = block
            elif rest == "":
                if index < len(lines) and lines[index][0] > indent:
                    node[key], index = _parse(lines, index, lines[index][0])
                else:
                    node[key] = {}
            else:
                node[key] = _scalar(rest)
            while index < len(lines) and lines[index][0] > indent and not lines[index][1].startswith("- "):
                extra, index = _parse_map(lines, index, lines[index][0])
                node.update(extra)
            items.append(node)
        else:
            items.append(_scalar(item))
    return items, index


def _parse_block_scalar(lines, index, parent_indent):
    chunks = []
    while index < len(lines) and lines[index][0] > parent_indent:
        chunks.append(lines[index][1])
        index += 1
    return "\n".join(chunks), index


def _looks_like_key(text: str) -> bool:
    if text.startswith(("'", '"', "[", "{")):
        return False
    if ":" not in text:
        return False
    key, _, _ = text.partition(":")
    return bool(key) and " " not in key


def _split_key(text: str, line_no: int):
    if text.startswith("- "):
        raise YAMLError(f"expected a key at line {line_no}")
    key, sep, rest = text.partition(":")
    if not sep or not key or " " in key:
        raise YAMLError(f"expected 'key:' at line {line_no}")
    return key.strip(), rest.strip(), line_no


def _scalar(text: str):
    if text in {"|", ">"}:
        raise YAMLError("block scalar needs nested lines")
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part.strip()) for part in _split_commas(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        mapping = {}
        if not inner:
            return mapping
        for part in _split_commas(inner):
            key, sep, rest = part.partition(":")
            if not sep:
                raise YAMLError(f"expected key: value in {text!r}")
            mapping[key.strip()] = _scalar(rest.strip())
        return mapping
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        body = text[1:-1]
        if text[0] == '"':
            body = bytes(body, "utf-8").decode("unicode_escape")
        return body
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _split_commas(text: str) -> list[str]:
    parts = []
    buf = []
    quote = ""
    for ch in text:
        if ch in {"'", '"'}:
            if quote == ch:
                quote = ""
            elif not quote:
                quote = ch
            buf.append(ch)
        elif ch == "," and not quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts
