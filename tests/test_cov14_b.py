"""Round 14 coverage: fleet - governor health failure is advisory, privileged target not allowed,
offload cancellation propagates, a non-privileged gateway refusal from remote_spawn is raised."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def fm(tmp_path):
    c = MagicMock()
    c.active = {}
    c.fleet_hops = 0
    return FleetManager(c, tmp_path)


def test_local_health_governor_raises():
    from avrone.governor import Governor
    g = Governor.__new__(Governor)
    g.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sensor"))
    assert fleet_mod.local_health(SimpleNamespace(governor=g)) is None


def test_privileged_target_not_allowed(fm, monkeypatch):
    monkeypatch.setattr(fm, "privileged", True, raising=False)
    monkeypatch.setattr(fm, "allowed", lambda n: False)
    with pytest.raises(GatewayRefused) as e:
        fm.check_outbound("run", "priv:shell", 0, "edge-1", privileged=True)
    assert e.value.code == "target_not_allowed"


def test_offload_cancel_propagates(fm, monkeypatch):
    async def hang(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(fm, "dispatch", hang)

    async def go():
        t = asyncio.create_task(fm.offload_worker("r", "edge", "W", "task", "focus"))
        await asyncio.sleep(0.05)
        t.cancel()
        await t
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())


def test_remote_spawn_plain_refusal_raises(fm, monkeypatch):
    async def refuse(*a, **k):
        raise GatewayRefused("hops", "max hops")
    monkeypatch.setattr(fm, "dispatch", refuse)
    ctx = SimpleNamespace(core=SimpleNamespace(fleet=fm, fleet_hops=0), run_id="r")
    with pytest.raises(GatewayRefused):
        asyncio.run(fleet_mod.t_remote_spawn(ctx, {"node": "edge", "task": "t"}))
