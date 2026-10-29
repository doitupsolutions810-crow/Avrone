import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import tools_extra as tx
from avrone import tools_fleetstatus as fs
from avrone.fleet import FleetManager
from avrone.governor import Governor
from avrone.tools import builtin_registry

TOKEN = "fleet-hmac-SUPERSECRET-0123456789abcdefXYZ"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_PRIVILEGED",
              "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_OFFLOAD",
              "AVRONE_SQUAD_DISPATCH", "AVRONE_AGENT_MIGRATE", "AVRONE_FLEET_MAX_HOPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_NODE_ID", "main-node")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = MagicMock()
    core.active = {}
    core.governor = None
    core.codex.emit = MagicMock()
    core.tools = reg
    fm = FleetManager(core, tmp_path)
    core.fleet = fm
    ctx = SimpleNamespace(core=core, run_id="r1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, fm=fm, core=core)


def call(env, **args):
    return run(env.reg.call(env.ctx, "fleet_status", args))


def add(fm, nid, age=0.0, health=None, load=0, runner="local", url="http://u:pw@10.0.0.9:8000/?token=abc"):
    fm.nodes = {**fm.nodes, nid: {"node_id": nid, "url": url, "last_seen": time.time() - age,
                                  "capabilities": {"runner": runner, "tools": ["shell"], "secret": TOKEN},
                                  "children": ["a", "b"], "load": load, "token": TOKEN,
                                  "presence": {"agents": [{"name": "X", "task": "private task text"}]},
                                  "health": health}}


def test_registered_with_schema(env):
    t = env.reg.get("fleet_status")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("fleet_status", {"limit": "x"})[0] == "bad_args"
    assert env.reg.check("fleet_status", {"limit": 3, "need": "docker"}) is None


def test_no_fleet_manager(env):
    env.core.fleet = None
    out = call(env)
    assert "no fleet manager on this node" in out
    rep = json.loads(call(env, format="json"))
    assert rep["fleet"] == "off" and rep["settings"] is None and rep["nodes"] == []


def test_disabled_fleet_empty(env):
    out = call(env)
    assert "fleet_status node main-node" in out
    assert "fleet=off" in out and "no child nodes registered" in out
    assert "(none eligible)" in out
    assert "this node health: unknown" in out


def test_presence_health_and_redaction(env):
    fm = env.fm
    fm.token = TOKEN
    fm.parent = "https://user:pass@parent.example:9/"
    fm.heartbeat_s = 15
    add(fm, "ok-node", health={"state": "ok", "headroom": 1.0, "load": 0.2, "temp_c": 40.0})
    add(fm, "hot-node", health={"state": "hot", "headroom": 0.1})
    add(fm, "thr-node", health={"state": "throttled", "headroom": 0.6}, load=3)
    add(fm, "deg-node", age=100, health={"state": "ok", "headroom": 1.0})
    add(fm, "off-node", age=10_000)
    out = call(env)
    assert "fleet=on" in out and "parent=on" in out and "privileged=off" in out
    assert "- ok-node [online/ok] score=" in out and "temp=40.0C" in out
    assert "- hot-node [online/hot]" in out and "- thr-node [online/throttled]" in out
    assert "- deg-node [degraded/stale]" in out and "- off-node [offline/unknown]" in out
    assert "known nodes: 5  eligible: 1" in out
    assert "would pick next: ok-node" in out
    joined = out + call(env, format="json")
    for bad in (TOKEN, "SUPERSECRET", "pw@", "pass@", "parent.example", "10.0.0.9", "token=abc",
                "private task", "http"):
        assert bad not in joined
    rep = json.loads(call(env, format="json"))
    assert rep["totals"]["by_status"] == {"online": 3, "degraded": 1, "offline": 1}
    assert rep["totals"]["by_state"]["hot"] == 1
    assert set(rep["settings"].values()) <= {"on", "off"}
    assert rep["nodes"][0]["children"] == 2 and rep["nodes"][0]["runner"] == "local"


def test_settings_on_off_only(env, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spread")
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "hybrid")
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    fm = env.fm
    s = json.loads(call(env, format="json"))["settings"]
    assert s["offload"] == s["network_dispatch"] == "off"  # effective: fleet disabled -> off
    fm.token = TOKEN
    fm.privileged = True
    fm.privileged_nodes = {"secret-host"}
    fm.allow = {"ok-node"}
    fm.require_allow = True
    s = json.loads(call(env, format="json"))["settings"]
    assert s["offload"] == s["network_dispatch"] == s["agent_migrate"] == "on"
    assert s["privileged"] == s["privileged_nodes"] == s["allowlist"] == s["require_allow"] == "on"
    assert s["hops"] == "on"
    fm.max_hops = 0
    assert json.loads(call(env, format="json"))["settings"]["hops"] == "off"
    out = call(env)
    assert "secret-host" not in out and "spread" not in out and "hybrid" not in out


def test_not_allowed_and_need_filter(env):
    fm = env.fm
    fm.allow = {"a-node"}
    add(fm, "a-node", health={"state": "ok", "headroom": 1.0}, runner="docker")
    add(fm, "b-node", health={"state": "ok", "headroom": 1.0})
    out = call(env)
    assert "b-node [online/ok]" in out and "NOT-ALLOWED" in out
    assert "would pick next: a-node" in out
    assert "would pick next for docker: a-node" in call(env, need="docker")
    assert "(none eligible)" in call(env, need="gpu")
    assert call(env, need="bad need!").startswith("ERROR: need")
    assert call(env, limit="x").startswith("ERROR: limit")
    assert call(env, limit=True).startswith("ERROR: limit")
    assert call(env, limit=[1]).startswith("ERROR: limit")


def test_limit_and_cap(env):
    fm = env.fm
    for i in range(60):
        add(fm, f"n{i:02d}" + "x" * 40, health={"state": "ok", "headroom": 1.0})
    raw = call(env, format="json", limit=500)
    assert len(raw) <= fs.FLEET_STATUS_MAX_CHARS
    rep = json.loads(raw)  # trimmed by whole rows, so still valid JSON
    assert len(rep["nodes"]) + rep["more"] == 60 and len(rep["nodes"]) < fs.FLEET_STATUS_MAX_LIMIT
    assert rep["more"] >= 10
    assert rep["totals"]["known"] == 60
    assert len(rep["would_pick"]) == fs.FLEET_STATUS_PICK
    out = call(env, limit=50)
    assert len(out) <= fs.FLEET_STATUS_MAX_CHARS + 20
    assert "...[10 more nodes" in out or out.endswith("...[truncated]")
    rep = json.loads(call(env, format="json", limit=0))
    assert len(rep["nodes"]) == 1


def test_tampered_records_are_sanitized(env):
    fm = env.fm
    fm.nodes = {"evil": {"node_id": "evil\n<script>" + TOKEN, "last_seen": "soon", "load": float("nan"),
                         "health": {"state": "ok", "headroom": 9e9, "temp_c": "hot"}, "capabilities": "x",
                         "children": 5},
                "junk": "not a dict"}
    out = call(env)
    assert "\n<script>" not in out and "evil??script?" in out
    rep = json.loads(call(env, format="json"))
    assert rep["totals"]["known"] == 1
    r = rep["nodes"][0]
    assert r["status"] == "offline" and r["load"] == 0.0 and r["children"] == 0 and r["runner"] == "?"
    assert len(r["node_id"]) <= 64


def test_select_nodes_failure_tolerated(env, monkeypatch):
    add(env.fm, "ok-node", health={"state": "ok", "headroom": 1.0})
    monkeypatch.setattr(env.fm, "select_nodes", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(env.fm, "allowed", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(env.fm, "offload_mode", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(env.fm, "squad_dispatch", MagicMock(side_effect=RuntimeError("boom")))
    rep = json.loads(call(env, format="json"))
    assert rep["would_pick"] == [] and rep["nodes"][0]["allowed"] is False
    assert rep["settings"]["offload"] == "off" and rep["settings"]["network_dispatch"] == "off"


def test_local_governor_health(env, tmp_path):
    gov = Governor(4, readers={"load": lambda: 0.1, "mem_pct": lambda: 10.0,
                               "disk_free_pct": lambda: 80.0, "temp_c": lambda: 45.0}, data_dir=str(tmp_path))
    env.core.governor = gov
    env.core.active = {"A": {"status": "working"}, "B": {"status": "idle"}}
    rep = json.loads(call(env, format="json"))
    lh = rep["local_health"]
    assert lh["state"] in ("ok", "throttled", "hot") and lh["active_agents"] == 1
    assert lh["temp_c"] == 45.0
    out = call(env)
    assert "this node health: " + lh["state"] in out and "temp=45.0C" in out
    env.core.fleet = None
    assert "this node health: " + lh["state"] in call(env)


def test_local_health_non_dict(env, monkeypatch):
    import avrone.fleet as fl
    monkeypatch.setattr(fl, "local_health", lambda core: {"state": "ok", "headroom": float("inf"),
                                                           "active_agents": True})
    lh = json.loads(call(env, format="json"))["local_health"]
    assert lh == {"state": "ok"}
