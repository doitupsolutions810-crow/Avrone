"""Tool-call robustness: arg schemas, unknown-tool suggestions, multi-call batches."""
import asyncio
import json

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import CORE_SCHEMAS, Tool, ToolRegistry, builtin_registry, validate_args


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ registry level
def test_validate_args_required_and_types():
    schema = {"required": {"path": "str", "n": "int"}, "optional": {"x": "float", "b": "str|dict"}}
    assert validate_args(schema, {"path": "a", "n": 2}) == []
    assert validate_args(schema, {"path": "a", "n": 2, "x": 1, "b": {"k": 1}, "extra": 1}) == []
    assert validate_args(schema, {"n": 2}) == ["missing 'path'"]
    assert validate_args(schema, {"path": None, "n": 2}) == ["missing 'path'"]
    probs = validate_args(schema, {"path": 3, "n": True, "x": "no", "b": [1]})
    assert "'path' must be str, got int" in probs
    assert "'n' must be int, got bool" in probs  # bools are not ints in a schema
    assert any(p.startswith("'x' must be float") for p in probs)
    assert any(p.startswith("'b' must be str|dict") for p in probs)
    assert validate_args(schema, ["not", "a", "dict"]) == ["args must be a JSON object, got list"]
    assert validate_args(None, "anything") == []
    assert validate_args({"required": {"q": "weird"}}, {"q": 1}) == []  # unknown type word never blocks


def test_core_tools_get_schemas():
    from avrone.tools_extra import register_extra_tools
    r = builtin_registry()
    register_extra_tools(r)
    for name in ("shell", "python", "read_file", "write_file", "edit_file", "fetch", "web_search", "spawn"):
        assert r.get(name).schema == CORE_SCHEMAS[name], name
    # plugin/MCP tools with a core name are not given a builtin schema
    r2 = ToolRegistry()
    r2.register(Tool("shell", "d", "{}", None, source="plugin:x"))
    assert r2.get("shell").schema is None
    # an explicit schema wins over the core one
    r2.register(Tool("fetch", "d", "{}", None, schema={"required": {"u": "str"}}))
    assert r2.get("fetch").schema == {"required": {"u": "str"}}


def test_check_messages():
    r = builtin_registry()
    reason, msg = r.check("read_file", {})
    assert reason == "bad_args"
    assert msg.startswith("ARG ERROR (read_file): missing 'path'; expected {\"path\": str}")
    reason, msg = r.check("shel", {"cmd": "ls"})
    assert reason == "unknown_tool" and "Did you mean: shell?" in msg
    assert "Did you mean: read_file" in r.check("readfile", {})[1]
    assert "Did you mean" not in r.check("zzzzzz", {})[1]
    assert r.check("shell", {"cmd": "ls"}) is None


def test_unknown_tool_call_suggests():
    r = builtin_registry()
    out = run(r.call(None, "Fetch", {}))
    assert out.startswith("ERROR: unknown tool 'Fetch'. Did you mean: fetch?")
    assert "Known:" in out
