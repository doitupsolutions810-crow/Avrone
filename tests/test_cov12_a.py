"""Round 12 coverage: fleet error branches (malformed messages, registry file failures, outbound gate
when disabled, memory sync caps and sender checks, migration target offline, thermal loop tick)."""
import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
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


@pytest.mark.parametrize("payload,match", [
    ("not a dict", "JSON object"), ({"agent": "A", "message": 5}, "'message'"),
    ({"agent": "  ", "message": "m"}, "'agent'"), ({"agent": "A", "message": "m", "route": {"a": 1}}, "'route'"),
])
def test_handle_message_rejects(fm, payload, match):
    with pytest.raises(ValueError, match=match):
        run(fm.handle_message(payload))


def test_corrupt_registry_cannot_be_moved(tmp_path, monkeypatch):
    (tmp_path / "fleet.json").write_text("{")
    real = fleet_mod.os.replace

    def replace(src, dst):
        if str(dst).endswith(".corrupt-" + str(dst).rsplit("-", 1)[-1]) and ".corrupt-" in str(dst):
            raise OSError("ro")
        return real(src, dst)
    monkeypatch.setattr(fleet_mod.os, "replace", replace)
    fm = FleetManager(MagicMock(), tmp_path)
    assert fm.nodes == {} and not list(tmp_path.glob("fleet.json.corrupt-*"))


def test_save_failure_cleans_tmp(fm, tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(fleet_mod.os, "replace", boom)
    with pytest.raises(OSError):
        fm._save()
    assert not list(tmp_path.glob(".fleet.json.*.tmp"))


def test_outbound_disabled(fm, monkeypatch):
    fm.token = ""
    monkeypatch.setattr(type(fm), "enabled", property(lambda self: False))
    with pytest.raises(GatewayRefused):
        fm.check_outbound("run", "squad", 0)


def test_push_memory_too_many(fm, monkeypatch):
    monkeypatch.setattr(fleet_mod, "MEMORY_SYNC_MAX_ENTRIES", 2)
    fm.parent = "http://parent.test"
    monkeypatch.setattr(fm, "check_outbound", lambda *a, **k: None)
    with pytest.raises(ValueError, match="too many entries"):
        run(fm.push_memory([{}, {}, {}], "root"))


def test_receive_memory_bad_sender(fm):
    with pytest.raises(ValueError, match="invalid sender"):
        fm.receive_memory({"from": "bad id!", "hops": 1, "capability": "memory:sync", "entries": []})


def test_migration_target_offline(fm, monkeypatch):
    fm.nodes["edge"] = {"node_id": "edge", "url": "http://e.test", "last_seen": time.time(),
                        "children": [], "capabilities": {}}
    monkeypatch.setattr(fm, "status_of", lambda n: "stale")
    with pytest.raises(RuntimeError, match="not online"):
        fm.pick_migration_target("edge")


def test_thermal_loop_ticks(fm, monkeypatch):
    calls = []

    async def migrate():
        calls.append(1)
        if len(calls) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(fleet_mod, "migrate_interval", lambda: 0)
    monkeypatch.setattr(fm, "thermal_migrate", migrate)
    with pytest.raises(asyncio.CancelledError):
        run(fm.thermal_loop())
    assert len(calls) == 2
