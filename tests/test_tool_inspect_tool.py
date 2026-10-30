import asyncio
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone import tools_introspect as ti
from avrone.tools import Tool, ToolRegistry, builtin_registry, validate_args


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env():
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace())
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx)


def call(env, **args):
    return run(env.reg.call(env.ctx, "inspect_tool", args))


def test_registered_as_builtin_with_schema(env):
    t = env.reg.get("inspect_tool")
    assert t is not None and t.source == "builtin"
    assert t.schema == {"optional": {"name": "str", "query": "str"}}
    assert env.reg.check("inspect_tool", {"name": 5})[0] == "bad_args"
    assert env.reg.check("inspect_tool", {"name": "shell"}) is None


def test_inspect_by_name(env):
    out = call(env, name="read_file")
    assert "tool: read_file" in out and "source: builtin" in out
    assert "module: avrone.tools" in out
    assert 'args: {"path": str}' in out
    assert "required: path: str" in out
    assert env.reg.get("read_file").description in out


def test_inspect_self_and_tool_without_schema(env):
    out = call(env, name="inspect_tool")
    assert "module: avrone.tools_introspect" in out and "optional: name: str, query: str" in out

    async def fn(ctx, args):
        return "x"
    env.reg.register(Tool("plug.thing", "a plugin tool", "{}", fn, source="plugin:demo"))
    out = call(env, name="plug.thing")
    assert "source: plugin:demo" in out and "schema: (none" in out


def test_unknown_suggests(env):
    out = call(env, name="read_fil")
    assert out.startswith("ERROR: unknown tool 'read_fil'") and "read_file" in out
    # never dumps the full registry listing
    assert "Known:" not in out


def test_query(env):
    out = call(env, query="MEMORY")
    assert "memory_save" in out and "memory_recall" in out
    assert "shell [" not in out
    assert call(env, query="zzz-no-such-thing").startswith("No tools match")


def test_query_bounded(env, monkeypatch):
    monkeypatch.setattr(ti, "INSPECT_MAX_MATCHES", 3)
    out = call(env, query="e")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 3 and "more; narrow the query" in out


def test_long_description_capped(env):
    async def fn(ctx, args):
        return ""
    env.reg.register(Tool("big", "d" * 50_000, "{}", fn))
    out = call(env, name="big")
    assert len(out) <= ti.INSPECT_MAX_CHARS + 20


@pytest.mark.parametrize("args", [{}, {"name": "shell", "query": "x"}, {"name": "  "}])
def test_exactly_one_arg(env, args):
    assert run(env.reg.call(env.ctx, "inspect_tool", args)) == "ERROR: give exactly one of name or query"


@pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "x" * 200, "shell;rm"])
def test_invalid_names(env, bad):
    assert call(env, name=bad) == "ERROR: invalid tool name"


def test_query_too_long(env):
    assert call(env, query="q" * 500).startswith("ERROR: query is longer")


def test_no_registry():
    reg = ToolRegistry()
    ti.register_introspect_tools(reg)
    ctx = SimpleNamespace(core=SimpleNamespace(), run_id="r", name="A", depth=0)
    assert run(reg.call(ctx, "inspect_tool", {"name": "shell"})) == "ERROR: the tool registry is not available"



@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("spec", ["int", "float"])
def test_boolean_values_are_not_accepted_as_numeric_tool_args(value, spec):
    # bool subclasses int in Python, but tool schemas intentionally distinguish them.
    errors = validate_args({"required": {"value": spec}}, {"value": value})
    assert errors and "must be " + spec in errors[0]
