"""Round 18 coverage: outbound gateway refuses a capability the request kind does not carry; a
cancelled netsquad worker re-raises the cancel and gives back the load it took on the peer."""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import GatewayRefused


@pytest.fixture
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_ALLOW",
              "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_MAX_NODES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    c = MagicMock()
    c.active = {}
    c.fleet_hops = 0
    return fleet_mod.FleetManager(c, tmp_path)


def test_outbound_capability_mismatch(fm):
    with pytest.raises(GatewayRefused) as e:
        fm.check_outbound("message", "spawn:squad", 0)
    assert e.value.code == "capability"


def test_netsquad_worker_cancel_restores_load(fm, monkeypatch):
    fm.register({"node_id": "edge", "url": "http://edge.test"})
    fm.nodes["edge"]["load"] = 3

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(fm, "_post", cancelled)
    monkeypatch.setattr(fleet_mod, "bridge_export", lambda *a, **k: [], raising=False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fm.netsquad_worker("r", "edge", "W", "t", "f"))
    assert fm.nodes["edge"]["load"] == 3
