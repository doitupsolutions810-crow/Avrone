import asyncio
import os
import time
from types import SimpleNamespace as S

import pytest

from avrone import server
from avrone import tools_limits as tl
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


def test_registered():
    r = builtin_registry()
    register_extra_tools(r)
    assert r.get("limits_status") and r.get("run_logs")
    from avrone.tools_statehealth import ACTING
    assert "run_logs" in ACTING and "limits_status" not in ACTING
    assert r.check("run_logs", {"keep": "x"})[0] == "bad_args"


def test_limits_status(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", "7")
    monkeypatch.setitem(server.state, "tasks", {1, 2})
    sem = asyncio.Semaphore(6)
    run(sem.acquire())
    c = S(core=S(settings=S(max_parallel=6), _sem=sem, active={"A": {"status": "working"}, "B": {"status": "done"}},
                 run_agents={"r1": 3, "r2": 9}))
    out = run(tl.t_limits_status(c, {}))
    assert "AVRONE_MAX_PARALLEL (concurrent model calls) = 6, 1 in use now" in out
    assert "AVRONE_MCP_MAX_INFLIGHT = 7 (env)" in out and "AVRONE_MAX_BACKGROUND_RUNS = 32 (default)" in out
    assert "2 background run(s) in flight of 32" in out and "1 agent(s) working, 2 known" in out
    assert "r2=9, r1=3" in out


def test_limits_status_bare_core(monkeypatch):
    monkeypatch.setattr(tl, "_limits", lambda: [])
    monkeypatch.delitem(server.state, "tasks", raising=False)
    out = run(tl.t_limits_status(S(core=None), {}))
    assert out.splitlines()[0] == "limits_status:" and all("live:" not in ln for ln in out.splitlines())


def test_limits_skips_broken_getter(monkeypatch):
    from avrone import ctxspawn
    monkeypatch.setattr(ctxspawn, "context_chars", lambda: 1 / 0)
    assert all(r[0] != "AVRONE_SPAWN_CONTEXT_CHARS" for r in tl._limits())


def _mk(runs, n, age_days):
    runs.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i in range(n):
        p = runs / f"run{i:03d}.jsonl"
        p.write_text("x" * 100)
        t = now - (age_days + i) * tl.DAY
        os.utime(p, (t, t))


def test_run_logs_report_and_prune(tmp_path):
    runs = tmp_path / "runs"
    _mk(runs, 80, 20)  # ages 20..99 days
    (runs / "link.jsonl").symlink_to(runs / "run000.jsonl")
    ctx = S(core=S(settings=S(data_dir=str(tmp_path)), active={"A": {"run_id": "run079"}}), run_id="run078")
    out = run(tl.t_run_logs(ctx, {}))
    assert out.startswith("run_logs: 80 run log(s)") and "oldest 99d ago" in out
    assert "prunable with older_than_days=30, keep=50: 30" in out
    out = run(tl.t_run_logs(ctx, {"prune": True}))
    assert "pruned 28 run log(s)" in out  # 30 stale minus run078 (this run) and run079 (active)
    left = sorted(p.stem for p in runs.glob("run*.jsonl"))
    assert len(left) == 52 and "run078" in left and "run079" in left and (runs / "link.jsonl").is_symlink()


def test_run_logs_floors_and_errors(tmp_path):
    runs = tmp_path / "runs"
    _mk(runs, 15, 100)
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))))
    out = run(tl.t_run_logs(ctx, {"prune": True, "keep": 0, "older_than_days": 0}))
    assert "pruned 5 run log(s) older than 1d (kept newest 10" in out
    assert run(tl.t_run_logs(ctx, {"keep": True})).startswith("ERROR")
    assert run(tl.t_run_logs(S(core=None), {})).startswith("run_logs: no data_dir")
    assert run(tl.t_run_logs(S(core=S(settings=S(data_dir=str(tmp_path / "x")))), {})) == "run_logs: no run logs yet"


def test_prune_unlink_failure(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _mk(runs, 12, 100)
    from pathlib import Path
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    assert tl.prune(runs, 1, 10, set()) == {"removed": 0, "freed": 0}


def test_limits_status_reports_body_and_fleet_caps(monkeypatch):
    import asyncio
    from types import SimpleNamespace as S
    from avrone.tools_limits import t_limits_status
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", "1")
    monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", "7")
    core = S(fleet=S(enabled=True, nodes={"a": {}, "b": {}}))
    out = asyncio.run(t_limits_status(S(core=core), {}))
    assert "- AVRONE_MAX_BODY_BYTES = " in out and "(env)" in out
    assert "- AVRONE_FLEET_MAX_NODES = 7 (env)" in out
    assert "body cap on /api/fleet/agentpack" in out and "live: 2 fleet peer(s) registered of 7" in out
    out = asyncio.run(t_limits_status(S(core=S(fleet=S(enabled=False, nodes={}))), {}))
    assert "fleet peer(s)" not in out


def test_limits_status_pack_cap_error_skipped(monkeypatch):
    import asyncio
    from types import SimpleNamespace as S
    from avrone import server
    from avrone.tools_limits import t_limits_status
    real = server.max_body_bytes
    monkeypatch.setattr(server, "max_body_bytes", lambda path="": (_ for _ in ()).throw(OSError()) if path else real())
    out = asyncio.run(t_limits_status(S(core=None), {}))
    assert "body cap on" not in out


def test_limits_status_reports_ws_subscribers(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace as S
    from avrone.codex import Codex
    from avrone.tools_limits import t_limits_status
    monkeypatch.setenv("AVRONE_MAX_WS_SUBSCRIBERS", "5")
    cx = Codex(tmp_path)
    cx.subscribe()
    out = asyncio.run(t_limits_status(S(core=S(codex=cx)), {}))
    assert "- AVRONE_MAX_WS_SUBSCRIBERS = 5 (env)" in out and "live: 1 event-stream subscriber(s) of 5" in out
