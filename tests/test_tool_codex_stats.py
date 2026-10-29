import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import tools_codexstats as cs
from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.tools import ToolRegistry, builtin_registry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    codex = Codex(tmp_path)
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, codex=codex, settings=SimpleNamespace())
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, codex=codex, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "codex_stats", args))


def seed(env):
    c = env.codex
    c.emit("run1", "run.start", "Trinity", prompt="secret plan AVRONE_FLEET_TOKEN=sk-abcdefghijklmnop1234")
    c.emit("run1", "tool.call", "Worker-1", tool="shell", args={"cmd": "echo hunter2-password"})
    c.emit("run1", "tool.result", "Worker-1", tool="shell", output="hunter2-password")
    c.emit("run1", "tool.call", "Worker-1", tool="read_file", args={"path": "x"})
    c.emit("run1", "tool.call", "Worker-1", tool="shell", args={"cmd": "ls"})
    c.emit("run1", "error", "Worker-1", message="boom TOKEN=abc")
    c.emit("run1", "tool.call", "Worker-2", tool="web_fetch", args={})
    c.emit("other", "tool.call", "Worker-1", tool="should_not_appear", args={})


def test_registered_with_schema(env):
    t = env.reg.get("codex_stats")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("codex_stats", {"last": "x"})[0] == "bad_args"
    assert env.reg.check("codex_stats", {"scope": "run", "last": 3}) is None


def test_self_scope_counts_and_redaction(env):
    seed(env)
    out = call(env)
    assert "agent Worker-1" in out
    assert "events: 5" in out
    assert "tool.call: 3" in out and "tool.result: 1" in out and "error: 1" in out
    assert "shell x2" in out and "read_file x1" in out
    assert "errors/refusals: 1" in out
    # never another agent's / run's events, never payloads
    for bad in ("web_fetch", "should_not_appear", "hunter2", "secret plan", "sk-abc", "TOKEN=", "boom"):
        assert bad not in out


def test_run_scope_and_json(env):
    seed(env)
    rep = json.loads(call(env, scope="run", format="json"))
    assert rep["events"] == 7 and rep["agent"] is None
    assert rep["by_kind"]["tool.call"] == 4 and rep["tools_called"]["web_fetch"] == 1
    assert "should_not_appear" not in json.dumps(rep)
    assert "hunter2" not in json.dumps(rep)
    assert rep["first"].endswith("Z") and rep["log_truncated"] is False


def test_kind_filter_and_last(env):
    seed(env)
    rep = json.loads(call(env, kind="tool.call", last=2, format="json"))
    assert rep["events"] == 3 and set(rep["by_kind"]) == {"tool.call"}
    assert [r["tool"] for r in rep["recent"]] == ["read_file", "shell"]
    rep = json.loads(call(env, last=0, format="json"))
    assert rep["recent"] == []
    rep = json.loads(call(env, last=10_000, format="json"))
    assert len(rep["recent"]) == 5


def test_empty_run(env):
    out = call(env)
    assert "events: 0" in out and "(none)" in out


@pytest.mark.parametrize("args,msg", [
    ({"scope": "everyone"}, "ERROR: scope"),
    ({"kind": "../etc"}, "ERROR: kind"),
    ({"kind": "k" * 100}, "ERROR: kind"),
    ({"last": True}, "ERROR: last"),
])
def test_bad_args(env, args, msg):
    assert call(env, **args).startswith(msg)


def test_run_id_from_context_only(env):
    seed(env)
    # args cannot select another run
    out = call(env, run_id="other")
    assert "should_not_appear" not in out
    env.ctx.run_id = "../../etc/passwd"
    assert call(env).startswith("ERROR: the codex log")


def test_symlinked_log_refused(env):
    target = env.tmp / "elsewhere.jsonl"
    target.write_text(json.dumps({"run_id": "run1", "kind": "x", "agent": "Worker-1", "ts": 1}) + "\n")
    os.symlink(target, env.codex.log_dir / "run1.jsonl")
    assert call(env).startswith("ERROR: the codex log")


def test_no_codex():
    reg = ToolRegistry()
    cs.register_codex_stats_tools(reg)
    ctx = SimpleNamespace(core=SimpleNamespace(), run_id="r", name="A", depth=0)
    assert run(reg.call(ctx, "codex_stats", {})).startswith("ERROR: the codex log")


def test_large_log_tail_and_corrupt_lines(env, monkeypatch):
    monkeypatch.setattr(cs, "CODEX_STATS_MAX_BYTES", 2000)
    p = env.codex.log_dir / "run1.jsonl"
    with p.open("w") as f:
        f.write("{not json\n[1,2]\n\n")
        for i in range(200):
            f.write(json.dumps({"run_id": "run1", "kind": "agent.think", "agent": "Worker-1", "ts": 1000 + i}) + "\n")
    rep = json.loads(call(env, format="json"))
    assert rep["log_truncated"] is True and 0 < rep["events"] < 200
    assert "only its tail" in call(env)


def test_hostile_labels_sanitised_and_output_capped(env, monkeypatch):
    p = env.codex.log_dir / "run1.jsonl"
    with p.open("w") as f:
        for i in range(500):
            f.write(json.dumps({"run_id": "run1", "kind": f"k{i}\n\x1b[31m" + "z" * 300,
                                "agent": "Worker-1", "ts": "bad"}) + "\n")
    out = call(env, last=50)
    assert "\x1b" not in out and len(out) <= cs.CODEX_STATS_MAX_CHARS + 20
    assert "more kinds" in out
    assert "?" in out  # bad timestamps render as '?'
