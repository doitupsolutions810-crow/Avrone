import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=False, allow_network=False)
    runner = LocalRunner(s)
    run(runner.start())
    codex = Codex(s.data_dir)
    core = SimpleNamespace(settings=s, runner=runner, codex=codex)
    ctx = SimpleNamespace(core=core, run_id="cur", name="Worker-1", depth=0)
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    return SimpleNamespace(codex=codex, ctx=ctx, reg=reg, core=core)


def call(env, **args):
    return run(env.reg.call(env.ctx, "codex_search", args))


def seed(env):
    c = env.codex
    c.emit("runA", "run.start", "Trinity", prompt="build a todo app")
    c.emit("runA", "tool.call", "Worker-1", tool="shell", args={"cmd": "pip install flask"}, step=1)
    c.emit("runA", "tool.result", "Worker-1", tool="shell", output="Successfully installed flask")
    c.emit("runA", "lead.plan", "Planner", squad_size=3)
    c.emit("runB", "tool.call", "Worker-2", tool="read_file", args={"path": "app.py"}, step=1)
    c.emit("runB", "error", "Worker-2", message="Boom: disk full")


def test_registered_with_doc(env):
    t = env.reg.get("codex_search")
    assert t is not None and t.source == "builtin"
    for k in ("kind", "agent", "tool", "query", "limit"):
        assert k in t.args_doc


def test_filter_by_kind_exact_and_prefix(env):
    seed(env)
    out = call(env, kind="tool.call")
    assert out.startswith("2 matching") and "pip install flask" in out and "app.py" in out
    assert "tool.result" not in out
    pre = call(env, kind="tool.")
    assert pre.startswith("3 matching")
    assert call(env, kind="lead*").startswith("1 matching")


def test_filter_by_agent_tool_query(env):
    seed(env)
    out = call(env, agent="worker-2")  # case-insensitive
    assert out.startswith("2 matching") and "[runB]" in out and "[runA]" not in out
    out = call(env, tool="shell")
    assert out.startswith("2 matching") and "tool=shell" in out
    out = call(env, query="DISK FULL")
    assert out.startswith("1 matching") and "error Worker-2" in out
    out = call(env, kind="tool.call", tool="shell", agent="Worker-1")
    assert out.startswith("1 matching")


def test_run_id_and_limit(env):
    seed(env)
    out = call(env, run_id="runB")
    assert out.startswith("2 matching") and "[runA]" not in out
    out = call(env, run_id="runA", limit=1)
    assert "showing 1" in out and len(out.splitlines()) == 2
    # newest first
    assert "lead.plan" in out


def test_runs_bound(env):
    seed(env)
    d = env.codex.log_dir
    os.utime(d / "runA.jsonl", (1, 1))  # make runA the oldest
    out = call(env, query="worker", runs=1)
    assert "[runB]" in out and "[runA]" not in out and "in 1 run(s)" in out


def test_no_matches_and_empty(env):
    assert "No CodeX runs logged yet" in call(env, kind="tool.call")
    seed(env)
    assert call(env, kind="nonexistent").startswith("No CodeX events match")


def test_corrupt_lines_skipped(env):
    seed(env)
    with (env.codex.log_dir / "runA.jsonl").open("a") as f:
        f.write("not json\n[1,2]\n" + json.dumps({"kind": "tool.call", "agent": "X", "data": "bad", "ts": "x"}) + "\n")
    out = call(env, kind="tool.call")
    assert out.startswith("3 matching") and " X " in out


def test_output_and_scan_caps(env, monkeypatch):
    for i in range(60):
        env.codex.emit("big", "tool.result", "W", tool="shell", output="y" * 3000)
    out = call(env, kind="tool.result", limit=200)
    assert len(out) <= tx.CODEX_MAX_OUTPUT_CHARS + 100 and "[output capped]" in out
    assert all(len(ln) < 400 for ln in out.splitlines())  # snippets truncated
    monkeypatch.setattr(tx, "CODEX_MAX_EVENTS_SCANNED", 5)
    out = call(env, kind="tool.result")
    assert out.startswith("5 matching") and "scan stopped" in out


def test_errors(env):
    seed(env)
    assert "at least one filter" in call(env)
    assert "invalid run_id" in call(env, run_id="../../etc/passwd")
    assert "invalid run_id" in call(env, run_id="..")
    assert "invalid run_id" in call(env, run_id="a/b")
    assert "not found" in call(env, run_id="nope")
    assert "longer than 500" in call(env, query="x" * 501)
    env.core.codex = None
    assert "not available" in call(env, kind="tool.call")


def test_read_only(env):
    seed(env)
    d = env.codex.log_dir
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    call(env, kind="tool.call")
    call(env, query="flask")
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
