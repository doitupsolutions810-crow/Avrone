"""Round 13 coverage: evolve tool wrappers' error/format branches, reconcile_interrupted write
failure, orphan detection, and the evolve_list partial-file tolerance."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import evolve


def run(c):
    return asyncio.run(c)


def _ectx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r", name="E", depth=0)


@pytest.fixture(autouse=True)
def _allow(monkeypatch):
    monkeypatch.setattr(evolve, "_denied", lambda: False)


CANDS = [[{"path": "a.txt", "content": "x"}]]


def test_start_tool_error_passthrough(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return {"error": "ERROR: busy"}
    monkeypatch.setattr(evolve, "evolve_start", fake)
    assert run(evolve.t_evolve_start(_ectx(tmp_path), {"goal": "g", "candidates": CANDS})) == "ERROR: busy"


def test_loop_tool_error_and_format(tmp_path, monkeypatch):
    async def err(*a, **k):
        return {"error": "ERROR: no git"}
    monkeypatch.setattr(evolve, "evolve_loop", err)
    assert run(evolve.t_evolve_loop(_ectx(tmp_path), {"goal": "g", "candidates": CANDS})) == "ERROR: no git"

    async def ok(*a, **k):
        return {"loop_id": "L1", "stopped": "passed", "duration_s": 1.5, "max_iters": 3, "time_budget_s": 60,
                "attempts": [{"iteration": 1, "pid": "p1", "error": "boom", "duration_s": None},
                             {"iteration": 2, "pid": "p2", "passed": True, "duration_s": 0.4, "branch": "evolve/p2"}]}
    monkeypatch.setattr(evolve, "evolve_loop", ok)
    out = run(evolve.t_evolve_loop(_ectx(tmp_path), {"goal": "g", "candidates": CANDS}))
    assert "loop L1: stopped=passed after 2 iteration(s)" in out and "#1 p1: ERROR boom [0.0s]" in out


def test_orphaned_and_effective(monkeypatch):
    assert evolve._orphaned({"status": "done", "owner_pid": 1}) is False
    assert evolve._orphaned({"status": "running"}) is False
    me = os.getpid()
    assert evolve._orphaned({"status": "running", "owner_pid": me, "owner_token": "other"}) is True
    assert evolve._orphaned({"status": "running", "owner_pid": me}) is False
    monkeypatch.setattr(evolve, "_pid_alive", lambda pid: False)
    lp = {"status": "running", "owner_pid": 999999}
    assert evolve._effective(lp)["status"] == "interrupted" and lp["status"] == "running"


def test_reconcile_write_failure_skips(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=str(tmp_path))
    d = evolve._settings_loops_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    (d / "L1.json").write_text(json.dumps({"loop_id": "L1", "status": "running", "owner_pid": 999999,
                                           "goal": "g", "max_iters": 2, "time_budget_s": 10}))
    monkeypatch.setattr(evolve, "_pid_alive", lambda pid: False)
    real = evolve.os.replace
    monkeypatch.setattr(evolve.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("ro")))
    assert evolve.reconcile_interrupted(settings) == []
    monkeypatch.setattr(evolve.os, "replace", real)
    assert evolve.reconcile_interrupted(settings) == ["L1"]


def test_list_skips_partial_and_fixes_files(tmp_path):
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    d.mkdir(parents=True, exist_ok=True)
    (d / "torn.json").write_text("{")
    (d / "partial.json").write_text(json.dumps({"pid": "x"}))
    (d / "ok.json").write_text(json.dumps({"pid": "p9", "passed": True, "goal": "g", "files": "nope", "by": "E",
                                           "loop_id": "L2", "iteration": 1}))
    out = run(evolve.t_evolve_list(ctx, {}))
    assert "p9" in out and "loop=L2#1" in out and "partial" not in out
