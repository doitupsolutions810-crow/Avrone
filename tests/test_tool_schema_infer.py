"""Builtin tools without a hand-written schema get a conservative one inferred from args_doc:
missing required scalar args become an ARG ERROR before the tool runs; optional keys stay lenient."""
import asyncio

import pytest

from avrone.tools import Tool, ToolRegistry, builtin_registry, infer_schema
from avrone.tools_extra import register_extra_tools


@pytest.mark.parametrize("doc,want", [
    ('{"host": str, "port"?: int}', {"required": {"host": "str"}}),
    ('{"path": str, "content": str, "append"?: bool}', {"required": {"path": "str", "content": "str"}}),
    ('{"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}', {"required": {"goal": "str"}}),
    ('{"action": "queue_stats"|"node_list", "run_id"?: str}', {}),
    ('{"host": str, "scheme"?: "https|http"}', {"required": {"host": "str"}}),
    ('{"limit"?: int}', {}), ("{}", {}),
    ('{"failures": int, "runs": int} or {"h": float}', None), ("not json-ish", None), ("", None),
])
def test_infer(doc, want):
    assert infer_schema(doc) == want


def test_every_builtin_has_schema_but_alternatives():
    r = builtin_registry()
    register_extra_tools(r)
    missing = [n for n in r.names() if r.get(n).schema is None]
    assert missing == ["great_scale"]


def test_inferred_schema_gates_missing_required():
    r = builtin_registry()
    register_extra_tools(r)
    reason, msg = r.check("tcp_ping", {})
    assert reason == "bad_args" and "missing 'host'" in msg
    assert r.check("tcp_ping", {"host": "example.com", "port": "443"}) is None  # optional stays lenient
    assert r.check("evolve_list", ["x"])[0] == "bad_args"


def test_explicit_and_nonbuiltin_untouched():
    async def f(ctx, args):
        return "ok"
    r = ToolRegistry()
    r.register(Tool("m", "d", '{"q": str}', f, source="mcp:x"))
    r.register(Tool("b", "d", '{"q": str}', f, schema={"optional": {"q": "str"}}))
    assert r.get("m").schema is None and r.get("b").schema == {"optional": {"q": "str"}}
