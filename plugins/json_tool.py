"""AVRONE plugin: validate, pretty-print, and query JSON with a dotted path."""
from __future__ import annotations

import json
import re

from avrone.tools import Tool

_INDEX = re.compile(r"^([^\[\]]+)?(?:\[(\d+)\])?$")


def _step(cur, token: str):
    if token.isdigit():
        i = int(token)
        if not isinstance(cur, list) or i >= len(cur):
            raise IndexError(token)
        return cur[i]
    m = _INDEX.fullmatch(token)
    if m is None or (m.group(1) is None and m.group(2) is None):
        raise ValueError(f"bad path segment: {token!r}")
    key, idx = m.group(1), m.group(2)
    if key is not None:
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(key)
        cur = cur[key]
    if idx is not None:
        i = int(idx)
        if not isinstance(cur, list) or i >= len(cur):
            raise IndexError(idx)
        cur = cur[i]
    return cur


def _walk(data, path: str):
    cur = data
    for part in path.split("."):
        if part == "":
            raise ValueError("empty path segment")
        cur = _step(cur, part)
    return cur


def _loads(raw: str):
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"ERROR: invalid JSON: {e.msg} (line {e.lineno} col {e.colno})"


async def json_tool(ctx, args: dict) -> str:
    action = str(args.get("action") or "validate").strip().lower()
    raw = args.get("text", args.get("json", ""))
    if not isinstance(raw, str):
        raw = json.dumps(raw)
    raw = raw.strip()
    if action not in {"validate", "pretty", "get"}:
        return "ERROR: action must be validate, pretty, or get"
    if not raw:
        return "ERROR: give JSON text in 'text'"
    data, err = _loads(raw)
    if action == "validate":
        return "valid" if err is None else err
    if err:
        return err
    if action == "pretty":
        indent = args.get("indent", 2)
        try:
            indent_n = int(indent)
        except (TypeError, ValueError):
            return "ERROR: indent must be an integer"
        if indent_n < 0 or indent_n > 8:
            return "ERROR: indent must be between 0 and 8"
        return json.dumps(data, indent=indent_n, ensure_ascii=False)
    path = str(args.get("path") or "").strip()
    if not path:
        return "ERROR: get requires 'path' (dotted, e.g. items[0].name)"
    try:
        found = _walk(data, path)
    except (KeyError, IndexError, ValueError, TypeError) as e:
        return f"ERROR: path '{path}': {e}"
    return json.dumps(found, ensure_ascii=False)


def register(registry, settings) -> None:
    registry.register(Tool(
        "json_tool",
        "validate JSON, pretty-print it, or get a value by a jq-like dotted path (a.b, items[0].name).",
        '{"action"?: "validate"|"pretty"|"get", "text": str, "path"?: str, "indent"?: int}',
        json_tool, source="plugin:json_tool"))
