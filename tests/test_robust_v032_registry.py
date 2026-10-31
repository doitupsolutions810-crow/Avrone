"""v0.32: the fleet peer registry is bounded. Before: any allowed id could register (allow-all by
default), with no limit on peers or record size; every join rewrote fleet.json, so a token holder
looping over fresh ids grew it without bound (quadratic I/O) and could park megabytes per peer."""
import time
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod


@pytest.fixture
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_ALLOW",
              "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_MAX_NODES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    c = MagicMock()
    c.active = {}
    return fleet_mod.FleetManager(c, tmp_path)


def test_max_nodes_env(monkeypatch):
    assert fleet_mod.max_nodes() == 256
    for v, want in (("3", 3), ("0", 1), ("99999", 4096), ("junk", 256)):
        monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", v)
        assert fleet_mod.max_nodes() == want


def test_full_registry_refuses_new_ids_but_not_rejoin(fm, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", "2")
    fm.register({"node_id": "a", "url": "http://a.test"})
    fm.register({"node_id": "b", "url": "http://b.test"})
    with pytest.raises(PermissionError, match="registry full"):
        fm.register({"node_id": "c", "url": "http://c.test"})
    fm.register({"node_id": "a", "url": "http://a.test"})  # heartbeat of a known peer still works
    assert set(fm.nodes) == {"a", "b"}


def test_offline_peers_evicted_oldest_first(fm, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_MAX_NODES", "2")
    fm.register({"node_id": "a", "url": "http://a.test"})
    fm.register({"node_id": "b", "url": "http://b.test"})
    fm.nodes["a"]["last_seen"] = 1
    fm.nodes["b"]["last_seen"] = 2
    fm.register({"node_id": "c", "url": "http://c.test"})
    assert set(fm.nodes) == {"b", "c"}


def test_oversized_record_refused(fm):
    with pytest.raises(ValueError, match="node record is"):
        fm.register({"node_id": "a", "url": "http://a.test", "capabilities": {"x": "y" * 70_000}})
    with pytest.raises(ValueError, match="not JSON"):
        fm.register({"node_id": "a", "url": "http://a.test", "capabilities": {(1, 2): 1}})
    assert "a" not in fm.nodes
