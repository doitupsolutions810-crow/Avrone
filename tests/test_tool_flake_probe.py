import asyncio
import itertools
import json
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone import tools_flakeprobe as fp
from avrone.greatscale import evidence_snapshot
from avrone.tools import Tool, builtin_registry

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "flake_probe", args))


def fake(env, name, fn, schema=None):
    env.reg.register(Tool(name, "fake", "{}", fn, source="builtin", schema=schema))


def test_registered_with_schema(env):
    t = env.reg.get("flake_probe")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("flake_probe", {})[0] == "bad_args"
    assert env.reg.check("flake_probe", {"tool": "great_scale", "runs": "x"})[0] == "bad_args"
    assert env.reg.check("flake_probe", {"tool": "great_scale", "runs": 3, "args": {}}) is None
    for name in fp.FLAKE_PROBE_ALLOWED:  # every allow-listed tool really exists
        assert env.reg.get(name) is not None, name


def test_deterministic_great_scale(env):
    out = call(env, tool="great_scale", runs=7, args={"failures": 1, "runs": 4})
    assert "flake_probe great_scale x7: deterministic" in out
    assert "distinct outputs: 1" in out and "x7 first at run 1" in out
    assert "cone floor: yes" in out and "orientation: pass" in out
    rep = json.loads(call(env, tool="great_scale", runs=3, args={"h": 0.5}, format="json"))
    assert rep["differing_runs"] == 0 and rep["runs"] == 3
    assert rep["great_scale"] == evidence_snapshot(failures=0, runs=3)
    # output content never echoed, only digests
    assert "heat" not in out and "Bernoulli" not in out


def test_inspect_tool_and_env_report_deterministic(env):
    out = call(env, tool="inspect_tool", args={"name": "read_file"}, runs=4)
    assert "deterministic" in out
    out = call(env, tool="env_report", runs=3)
    assert "deterministic" in out


def test_nondeterministic_counts_and_snapshot(env):
    counter = itertools.count()

    async def flaky(ctx, args):
        i = next(counter)
        return "line a\nline b\nstable" if i % 3 else "line a\nline B " + SECRET + "\nstable"

    fake(env, "codex_stats", flaky)
    rep = json.loads(call(env, tool="codex_stats", runs=6, format="json"))
    # run 1 is the "odd" one (i=0), runs 2,3,5,6 differ from it, run 4 (i=3) matches
    assert rep["differing_runs"] == 4 and rep["distinct_outputs"] == 2
    assert [d["run"] for d in rep["diffs"]] == [2, 3, 5, 6]
    assert all(d["first_diff_line"] == 2 for d in rep["diffs"])
    assert rep["great_scale"] == evidence_snapshot(failures=4, runs=6)
    assert rep["great_scale"]["orientation"] == "mixed"
    assert SECRET not in json.dumps(rep)
    counter2 = itertools.count()

    async def flaky2(ctx, args):
        return f"v{next(counter2) % 2}"

    fake(env, "codex_stats", flaky2)
    out = call(env, tool="codex_stats", runs=4)
    assert "NONDETERMINISTIC (2/4 runs differed from run 1)" in out
    assert "#2@line 1, #4@line 1" in out and "cone floor: no" in out
    assert "v0" not in out and "v1" not in out


def test_non_string_output_compared(env):
    async def num(ctx, args):
        return 42

    fake(env, "codex_stats", num)
    rep = json.loads(call(env, tool="codex_stats", runs=2, format="json"))
    assert rep["differing_runs"] == 0 and rep["outputs"][0]["chars"] == 2


def test_args_passed_fresh_each_run(env):
    seen = []

    async def mutating(ctx, args):
        seen.append(dict(args))
        args["poison"] = True
        return "same"

    fake(env, "memory_peek", mutating)
    rep = json.loads(call(env, tool="memory_peek", runs=3, args={"limit": 2}, format="json"))
    assert seen == [{"limit": 2}] * 3 and rep["differing_runs"] == 0


def test_errors_and_line_diff_lengths(env):
    outs = iter(["ERROR: x", "ERROR: x\nmore", "ok"])

    async def f(ctx, args):
        return next(outs)

    fake(env, "self_scan", f)
    rep = json.loads(call(env, tool="self_scan", runs=3, format="json"))
    assert rep["errors"] == 2 and rep["differing_runs"] == 2
    assert rep["diffs"][0] == {"run": 2, "first_diff_line": 2}
    assert rep["diffs"][1] == {"run": 3, "first_diff_line": 1}


def test_refusals(env):
    assert "recursion refused" in call(env, tool="flake_probe")
    for bad in ("shell", "write_file", "web_fetch", "fleet_status", "priv_shell"):
        out = call(env, tool=bad)
        assert "not on the flake_probe allow-list" in out
    assert call(env).startswith("ERROR: give tool")
    assert "runs must be 1..50" in call(env, tool="great_scale", runs=51)
    assert "runs must be 1..50" in call(env, tool="great_scale", runs=0)
    assert "runs must be an integer" in call(env, tool="great_scale", runs="many")
    assert "runs must be an integer" in call(env, tool="great_scale", runs=True)
    assert "args must be an object" in call(env, tool="great_scale", args=[1])
    assert "longer than" in call(env, tool="great_scale", args={"x": "y" * 5000})
    assert "plain JSON" in call(env, tool="great_scale", args={"x": object()})
    # schema of the target is enforced before running it
    out = call(env, tool="codex_stats", args={"last": "x"})
    assert "rejected by codex_stats's schema" in out
    # default runs and None runs
    assert "x5:" in call(env, tool="great_scale", args={"h": 0.1})
    assert "x5:" in call(env, tool="great_scale", runs=None, args=None)


def test_unregistered_and_no_registry(env):
    env.reg.unregister("self_scan")
    assert "not registered" in call(env, tool="self_scan")
    ctx = SimpleNamespace(core=SimpleNamespace(tools=None))
    out = run(fp.t_flake_probe(ctx, {"tool": "great_scale"}))
    assert "registry is not available" in out


def test_alias_to_self_refused(env):
    env.reg.register(Tool("great_scale", "alias", "{}", fp.t_flake_probe, source="builtin"))
    assert "recursion refused" in call(env, tool="great_scale")


def test_timeout_and_budget(env):
    async def slow(ctx, args):
        await asyncio.sleep(1)
        return "never"

    fake(env, "capabilities_report", slow)
    rep = run(fp.flake_probe(env.ctx, env.reg, "capabilities_report", 2, {}, run_timeout=0.01))
    assert rep["timeouts"] == 2 and rep["differing_runs"] == 0 and rep["outputs"][0]["digest"] == "timeout"
    n = itertools.count()

    async def half(ctx, args):
        if next(n) == 1:
            await asyncio.sleep(1)
        return "x"

    fake(env, "capabilities_report", half)
    rep = run(fp.flake_probe(env.ctx, env.reg, "capabilities_report", 3, {}, run_timeout=0.05))
    assert rep["timeouts"] == 1 and rep["differing_runs"] == 1 and rep["diffs"] == [{"run": 2, "first_diff_line": None}]
    rep = run(fp.flake_probe(env.ctx, env.reg, "great_scale", 50, {"h": 0.2}, budget=0.0))
    assert rep["runs"] == 1 and rep["budget_hit"] and rep["runs_requested"] == 50


def test_text_timeout_and_budget_rendering(env, monkeypatch):
    async def fake_probe(ctx, reg, name, runs, args):
        return {"tool": name, "runs_requested": runs, "runs": 2, "budget_hit": True, "differing_runs": 1,
                "distinct_outputs": 2, "outputs": [{"digest": "abc", "chars": 1, "runs": 1, "first_run": 1},
                                                   {"digest": "timeout", "chars": 0, "runs": 1, "first_run": 2}],
                "outputs_hidden": 3, "diffs": [{"run": 2, "first_diff_line": None}], "diffs_hidden": 4,
                "errors": 0, "timeouts": 1, "ms": {"min": 1, "max": 2, "total": 3},
                "great_scale": evidence_snapshot(failures=1, runs=2)}

    monkeypatch.setattr(fp, "flake_probe", fake_probe)
    out = call(env, tool="great_scale", runs=9, args={"h": 0})
    assert "x2 (of 9; time budget hit)" in out and "#2 (timeout)" in out
    assert "[3 more distinct outputs]" in out and "...[+4]" in out


def test_output_capped(env, monkeypatch):
    monkeypatch.setattr(fp, "FLAKE_PROBE_MAX_CHARS", 100)
    out = call(env, tool="great_scale", runs=2, args={"h": 0.5})
    assert out.endswith("...[truncated]") and len(out) <= 120


def test_many_distinct_outputs_bounded(env):
    c = itertools.count()

    async def unique(ctx, args):
        return f"out-{next(c)}"

    fake(env, "codex_stats", unique)
    rep = json.loads(call(env, tool="codex_stats", runs=50, format="json"))
    assert rep["differing_runs"] == 49 and rep["distinct_outputs"] == 50
    assert len(rep["outputs"]) == fp.FLAKE_PROBE_MAX_LISTED and rep["outputs_hidden"] == 30
    assert len(rep["diffs"]) == fp.FLAKE_PROBE_MAX_LISTED and rep["diffs_hidden"] == 29
    assert rep["great_scale"]["runs"] == 50 and rep["great_scale"]["failures"] == 49
