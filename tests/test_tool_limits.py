import asyncio
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
    assert r.get("limits_status")
    from avrone.tools_statehealth import ACTING
    assert "limits_status" not in ACTING


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
    assert run(tl.t_limits_status(S(core=None), {})) == "limits_status:"


def test_limits_skips_broken_getter(monkeypatch):
    from avrone import ctxspawn
    monkeypatch.setattr(ctxspawn, "context_chars", lambda: 1 / 0)
    assert all(r[0] != "AVRONE_SPAWN_CONTEXT_CHARS" for r in tl._limits())
