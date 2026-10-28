"""edgeroute: governor-aware fleet routing (health in heartbeats, select_nodes, auto pick)."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import avrone.fleet as fleet_mod
from avrone.fleet import FleetManager, health_score, local_health, sanitize_health
from avrone.governor import Governor


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {}
    c.tools.names.return_value = ["shell", "web_fetch"]
    c.runner.kind = "local"
    c.codex.emit = MagicMock()
    return c


@pytest.fixture
def fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    for k in ("AVRONE_FLEET_ALLOW", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_OFFLOAD"):
        monkeypatch.delenv(k, raising=False)
    return FleetManager(core, tmp_path)


def add(fm, nid, age=0.0, load=0, health=None, tools=None):
    fm.nodes[nid] = {"node_id": nid, "url": f"http://{nid}.test", "last_seen": time.time() - age, "load": load,
                     "capabilities": {"runner": "local", "tools": tools or ["shell"]}, "children": [],
                     "health": health}


COOL = {"state": "ok", "headroom": 1.0, "load": 0.1, "temp_c": 45.0, "active_agents": 0}
WARM = {"state": "ok", "headroom": 1.0, "load": 0.9, "temp_c": 60.0, "active_agents": 3}
HOT = {"state": "hot", "headroom": 0.1, "load": 0.5, "temp_c": 84.0, "active_agents": 0}
THROTTLED = {"state": "throttled", "headroom": 0.6, "load": 1.4, "active_agents": 1}


# ---------------------------------------------------------------- health block
def gov(**vals):
    readers = {k: (lambda v=v: v) for k, v in vals.items()}
    for k in ("load", "mem_pct", "disk_free_pct", "temp_c"):
        readers.setdefault(k, lambda: None)
    return Governor(max_parallel=4, min_parallel=1, thermal_max_c=85, readers=readers, ttl_s=0)


def test_local_health_states(core):
    core.governor = gov(load=0.2, mem_pct=40, disk_free_pct=50, temp_c=50)
    core.active = {"A-1": {"status": "working"}, "B-2": {"status": "done"}}
    h = local_health(core)
    assert h["state"] == "ok" and h["headroom"] == 1.0 and h["active_agents"] == 1
    assert h["temp_c"] == 50 and h["load"] == 0.2 and h["mem_pct"] == 40 and h["disk_free_pct"] == 50
    core.governor = gov(load=1.5)
    assert local_health(core)["state"] == "throttled"
    core.governor = gov(temp_c=84)
    assert local_health(core)["state"] == "hot"
    core.governor = gov(temp_c=90)
    assert local_health(core)["state"] == "hot"
    json.dumps(local_health(core))


def test_local_health_unknown_governor_is_none(core):
    assert local_health(core) is None  # MagicMock governor: not a Governor
    core.governor = gov()
    assert local_health(core)["state"] == "ok"  # nothing readable = healthy, like target()


def test_sanitize_health_drops_junk():
    assert sanitize_health("nope") is None and sanitize_health({}) is None
    h = sanitize_health({"state": "evil", "headroom": 7, "load": float("nan"), "mem_pct": True,
                         "temp_c": 900, "active_agents": -3, "x": 1, "disk_free_pct": "9"})
    assert h == {"headroom": 1.0, "temp_c": 150.0, "active_agents": 0}


def test_register_and_heartbeat_carry_health(fm, core, monkeypatch):
    fm.register({"node_id": "kid", "url": "http://kid.test", "health": {**COOL, "junk": "x"}})
    assert fm.nodes["kid"]["health"] == {k: v for k, v in COOL.items()}
    fm.register({"node_id": "old", "url": "http://old.test"})  # pre-edgeroute node
    assert fm.nodes["old"]["health"] is None

    core.governor = gov(load=0.3, temp_c=55)
    fm.parent, fm.public_url = "http://parent.test", "http://me.test"
    import avrone.presence as presence
    monkeypatch.setattr(presence, "snapshot", AsyncMock(return_value={"node_id": "root"}))
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    run(fm.join_parent_once())
    assert sent["health"]["state"] == "ok" and sent["health"]["temp_c"] == 55


def test_health_goes_stale_with_status(fm):
    add(fm, "kid", health=COOL)
    assert fm.node_state(fm.nodes["kid"]) == "ok"
    fm.nodes["kid"]["last_seen"] = time.time() - 5 * fm.heartbeat_s  # degraded
    assert fm.node_state(fm.nodes["kid"]) == "stale"
    row = fm.list_nodes()[0]
    assert row["status"] == "degraded" and row["health"]["stale"] is True
    assert fm.nodes["kid"]["health"].get("stale") is None  # stored record untouched
    add(fm, "bare")
    assert fm.node_state(fm.nodes["bare"]) == "unknown"


# ---------------------------------------------------------------- scoring / selection
def test_health_score_orders_cool_idle_first():
    cool, warm = {"health": COOL, "load": 0}, {"health": WARM, "load": 0}
    unknown, busy = {"health": None, "load": 0}, {"health": COOL, "load": 6}
    assert health_score(cool) > health_score(warm) > health_score(unknown)
    assert health_score(cool) > health_score(busy)
    assert health_score(cool) == 100.0 - 20 * 0.1 / 2
    assert 0 <= health_score({"health": HOT, "load": 99}) <= 100


def test_select_nodes_ranks_and_excludes(fm):
    add(fm, "warm", health=WARM)
    add(fm, "cool", health=COOL)
    add(fm, "hot", health=HOT)
    add(fm, "throttled", health=THROTTLED)
    add(fm, "stale", age=5 * fm.heartbeat_s, health=COOL)  # degraded: excluded
    add(fm, "gone", age=1000, health=COOL)
    add(fm, "legacy")  # no health: eligible, ranked last
    assert [n["node_id"] for n in fm.select_nodes(10)] == ["cool", "warm", "legacy"]
    assert [n["node_id"] for n in fm.select_nodes(1)] == ["cool"]
    assert fm.select_nodes(0) == []
    fm.allow = {"warm", "hot", "legacy"}
    assert [n["node_id"] for n in fm.select_nodes(5)] == ["warm", "legacy"]


def test_select_nodes_deterministic_tiebreak_and_need(fm):
    for nid in ("c", "a", "b"):
        add(fm, nid, health=COOL)
    fm.nodes["b"]["capabilities"]["tools"] = ["shell", "gpu_infer"]
    assert [n["node_id"] for n in fm.select_nodes(3)] == ["a", "b", "c"]
    assert [n["node_id"] for n in fm.select_nodes(3, need="gpu_infer")] == ["b"]
    assert len(fm.select_nodes(3, need="spawn:chat")) == 3
    assert len(fm.select_nodes(3, need="local")) == 3  # runner kind
    assert fm.select_nodes(3, need="docker") == []


def test_pick_auto_uses_health(fm):
    with pytest.raises(RuntimeError, match="no online nodes"):
        fm.pick("auto")
    add(fm, "hot", health=HOT)
    with pytest.raises(RuntimeError, match="no healthy online nodes"):
        fm.pick("auto")
    add(fm, "warm", health=WARM, load=0)
    add(fm, "cool", health=COOL, load=1)
    assert fm.pick("auto")["node_id"] == "cool"
    assert fm.pick("hot")["node_id"] == "hot"  # explicit target still allowed


def test_health_table_and_tool_and_telemetry(fm, core):
    add(fm, "cool", health=COOL)
    add(fm, "hot", health=HOT)
    add(fm, "gone", age=1000, health=COOL)
    rows = {r["node_id"]: r for r in fm.health_table()}
    assert rows["cool"]["eligible"] and rows["cool"]["score"] == health_score(fm.nodes["cool"])
    assert not rows["hot"]["eligible"] and rows["hot"]["state"] == "hot"
    assert rows["gone"]["state"] == "stale" and rows["gone"]["score"] is None

    core.fleet = fm
    out = run(fleet_mod.t_fleet_nodes(SimpleNamespace(core=core), {}))
    assert "cool [online]" in out and "health=ok" in out and "health=hot" in out and "temp=84.0C" in out

    from avrone.telemetry import fleet_health
    assert [r["node_id"] for r in fleet_health(core)] == ["cool", "gone", "hot"]
    assert fleet_health(SimpleNamespace()) == []
