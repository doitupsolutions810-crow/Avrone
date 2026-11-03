import asyncio
import os
from types import SimpleNamespace

import pytest

from avrone.trinity import TrinityCore


class FakeCodex:
    def __init__(self):
        self.events = []
    def emit(self, *args, **kwargs):
        self.events.append((args, kwargs))


@pytest.mark.asyncio
async def test_explicit_swarm_dispatch_bypasses_local_planner_and_workers(monkeypatch):
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=4)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    called = {}
    async def dispatch(task, run_id, checkpoint, correlation_id):
        called.update(task=task, run_id=run_id, checkpoint=checkpoint, correlation_id=correlation_id)
        return {"run_id": run_id, "status": "queued", "answer": "queued", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("local planner must not run during explicit Swarm dispatch")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("task", run_id="run-1", dispatch="swarm", swarm_correlation_id="corr-1")
    assert result["status"] == "queued"
    assert called == {"task": "task", "run_id": "run-1", "checkpoint": None, "correlation_id": "corr-1"}


@pytest.mark.asyncio
async def test_environment_swarm_dispatch_is_explicit_and_no_local_fallback(monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=2)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    calls = []
    async def dispatch(*args):
        calls.append(args)
        return {"run_id": args[1], "status": "pending", "answer": "pending", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("local planner/worker fallback is forbidden")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("task", run_id="run-2")
    assert result["status"] == "pending"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_dispatch_value_is_rejected():
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=1)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    with pytest.raises(ValueError, match="dispatch must be"):
        await core.squad("task", run_id="run-3", dispatch="magic")
