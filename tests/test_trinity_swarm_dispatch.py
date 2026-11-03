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
    core.settings = SimpleNamespace(squad_size=3)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    calls = []
    async def dispatch(task, run_id, correlation_id):
        calls.append((task, run_id, correlation_id))
        return {"run_id": run_id, "status": "queued", "answer": "queued", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Swarm mode must not enter local planning or worker execution")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("objective", run_id="run-1", dispatch="swarm",
                              swarm_correlation_id="stable-correlation")
    assert result["status"] == "queued"
    assert calls == [("objective", "run-1", "stable-correlation")]


@pytest.mark.asyncio
async def test_environment_swarm_dispatch_bypasses_local_path(monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=2)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], {"status": "stale"})
    calls = []
    async def dispatch(task, run_id, correlation_id):
        calls.append((task, run_id, correlation_id))
        return {"run_id": run_id, "status": "pending", "answer": "pending", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("cached checkpoint/local path must not override Swarm outbox")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("objective", run_id="run-2")
    assert result["status"] == "pending"
    assert calls == [("objective", "run-2", None)]


@pytest.mark.asyncio
async def test_unknown_dispatch_value_is_rejected():
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=1)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    with pytest.raises(ValueError, match="dispatch must be"):
        await core.squad("objective", run_id="run-3", dispatch="magic")


@pytest.mark.asyncio
async def test_privileged_run_rejected_before_reading_secrets(monkeypatch):
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: True
    result = await core._dispatch_swarm("do not export", "run-4")
    assert result["status"] == "rejected"
    assert "privileged" in result["swarm"]["reason"]


@pytest.mark.asyncio
async def test_missing_parent_capability_fails_closed_without_local_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("AVRONE_CA_PARENT_CAPABILITY_FILE", raising=False)
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("objective", "run-5")
    assert result["status"] == "disabled"
    assert "AVRONE_CA_PARENT_CAPABILITY_FILE" in result["swarm"]["reason"]
    assert result["agents"] == []


@pytest.mark.asyncio
async def test_queue_acceptance_is_not_reported_as_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(tmp_path / "missing.json"))
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("objective", "run-6")
    assert result["status"] == "disabled"
    assert "completed" not in result["answer"].lower()
    assert result["agents"] == []
