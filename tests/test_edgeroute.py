"""edgeroute: governor-aware fleet routing (health in heartbeats, select_nodes, auto pick)."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import avrone.fleet as fleet_mod
from avrone.fleet import FleetManager, health_score, local_health, sanitize_health, sanitize_peer_load
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


def test_sanitize_health_clamps_arbitrary_size_integer_fields():
    huge = 10 ** 10000
    health = sanitize_health({
        "load": huge,
        "mem_pct": -huge,
        "disk_free_pct": huge,
        "temp_c": -huge,
        "headroom": huge,
        "active_agents": huge,
    })
    assert health == {
        "load": 1000.0,
        "mem_pct": 0.0,
        "disk_free_pct": 100.0,
        "temp_c": -40.0,
        "headroom": 1.0,
        "active_agents": 10_000,
    }


@pytest.mark.parametrize("raw,expected", [
    (10 ** 10000, 10_000),
    (-(10 ** 10000), 0),
    (float("inf"), 0),
    (float("-inf"), 0),
    (float("nan"), 0),
    (True, 0),
    ("huge", 0),
    (-5, 0),
    (5, 5),
])
def test_sanitize_peer_load_is_finite_and_bounded(raw, expected):
    assert sanitize_peer_load(raw) == expected


def test_registration_and_health_score_handle_extreme_peer_load(fm):
    fm.register({"node_id": "extreme", "url": "http://extreme.test",
                 "load": 10 ** 10000, "health": COOL})
    node = fm.nodes["extreme"]
    assert node["load"] == 10_000
    assert health_score(node) >= 0
    node["load"] = float("inf")  # protects routing against older/corrupt persisted records
    assert health_score(node) >= 0
    assert fm.health_table()[0]["load"] >= 0


def test_peer_load_decrement_sanitizes_before_arithmetic():
    assert max(0, sanitize_peer_load("corrupt") - 1) == 0
    assert max(0, sanitize_peer_load(float("inf")) - 1) == 0
    assert max(0, sanitize_peer_load(9) - 1) == 8


def test_pick_migration_target_sanitizes_legacy_peer_metrics(fm):
    # Simulate a fleet.json record written before peer-health/load normalization.
    add(fm, "legacy", load=10 ** 10000, health={
        "state": "ok", "headroom": 10 ** 10000, "load": 10 ** 10000,
        "temp_c": 10 ** 10000, "active_agents": 10 ** 10000,
    })
    assert fm.pick_migration_target("auto")["node_id"] == "legacy"


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


# ================================================================ offload (AVRONE_FLEET_OFFLOAD)
from avrone.codex import Codex  # noqa: E402
from avrone.config import Settings  # noqa: E402
from avrone.sovereign import LocalRunner  # noqa: E402
from avrone.trinity import TrinityCore  # noqa: E402


def sensors(temp):
    return {"load": lambda: 0.2, "mem_pct": lambda: 30.0, "disk_free_pct": lambda: 60.0, "temp_c": lambda: temp}


class TrackLLM:
    def __init__(self, n):
        self.n, self.live, self.peak, self.workers = n, 0, 0, 0

    async def chat(self, messages, **kw):
        sys = messages[0]["content"]
        if "PLANNER" in sys:
            return json.dumps({"subtasks": [{"role": f"W{i}", "focus": f"focus-{i}"} for i in range(self.n)]})
        if kw.get("lead"):
            return "LEAD " + "x" * 400
        self.workers += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.02)
        self.live -= 1
        return '{"final": "ok"}'


@pytest.fixture
def squad_core(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    for k in ("AVRONE_FLEET_ALLOW", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_OFFLOAD"):
        monkeypatch.delenv(k, raising=False)

    def make(n=4, temp=95.0):
        s = Settings()
        s.data_dir, s.workspace, s.max_parallel, s.governor = tmp_path / "d", tmp_path / "ws", 6, True
        llm = TrackLLM(n)
        c = TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))
        c.governor = Governor(6, min_parallel=1, thermal_max_c=85, readers=sensors(temp), ttl_s=0)
        c.fleet = FleetManager(c, tmp_path / "fleet")
        return c, llm
    return make


def remote(monkeypatch, handler):
    calls = []

    async def h(req):
        calls.append(json.loads(req.content))
        r = handler(req)
        return await r if asyncio.iscoroutine(r) else r
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(h))
    return calls


def ok_handler(req):
    return httpx.Response(200, json={"node": req.url.host, "path": [req.url.host], "answer": f"remote:{req.url.host}"})


def fleet_events(core, kind):
    path = core.codex.log_dir / "fleet.jsonl"
    if not path.exists():
        return []
    return [e for e in map(json.loads, path.read_text().splitlines()) if e["kind"] == kind]


def test_offload_off_is_unchanged(squad_core, monkeypatch):
    core, llm = squad_core()
    add(core.fleet, "edge", health=COOL)
    calls = remote(monkeypatch, ok_handler)
    out = run(core.squad("task", n=4, run_id="off"))
    assert [a["result"] for a in out["agents"]] == ["ok"] * 4 and llm.peak == 1 and llm.workers == 4
    assert calls == [] and fleet_events(core, "fleet.offload") == []
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "bogus")  # unknown value = off
    run(core.squad("task", n=4, run_id="off2"))
    assert calls == []


def test_offload_spill_sends_overflow_to_best_node(squad_core, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spill")
    core, llm = squad_core()
    add(core.fleet, "warm", health=WARM)
    add(core.fleet, "cool", health=COOL)
    add(core.fleet, "hot", health=HOT)
    calls = remote(monkeypatch, ok_handler)
    out = run(core.squad("the task", n=4, run_id="spill"))
    res = [a["result"] for a in out["agents"]]
    assert res == ["ok", "remote:cool.test", "remote:cool.test", "remote:cool.test"]
    assert llm.workers == 1 and len(calls) == 3
    assert all(c["mode"] == "chat" and c["from"] == "root" and c["hops"] == 1 and c["origin_run"] == "spill"
               and "privileged" not in c for c in calls)
    assert sorted(c["prompt"].split("YOUR FOCUS: ")[1].split("\n")[0] for c in calls) == ["focus-1", "focus-2", "focus-3"]
    assert all("TASK: the task" in c["prompt"] for c in calls)
    ev = fleet_events(core, "fleet.offload")
    assert len(ev) == 3 and {e["data"]["node"] for e in ev} == {"cool"} and ev[0]["data"]["squad_run"] == "spill"
    assert fleet_events(core, "fleet.offload_fallback") == []


def test_offload_spread_round_robins(squad_core, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spread")
    core, llm = squad_core()
    add(core.fleet, "a", health=COOL)
    add(core.fleet, "b", health=COOL)
    remote(monkeypatch, ok_handler)
    out = run(core.squad("t", n=4, run_id="spread"))
    assert [a["result"] for a in out["agents"]] == ["ok", "remote:a.test", "remote:b.test", "remote:a.test"]


def test_offload_spill_falls_back_locally_on_remote_error(squad_core, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spill")
    core, llm = squad_core()
    add(core.fleet, "edge", health=COOL)
    calls = remote(monkeypatch, lambda r: httpx.Response(502, text="down"))
    out = run(core.squad("t", n=4, run_id="fb"))
    assert [a["result"] for a in out["agents"]] == ["ok"] * 4
    assert len(calls) == 3 and llm.workers == 4 and llm.peak == 1  # fallbacks still respect the governor gate
    fb = fleet_events(core, "fleet.offload_fallback")
    assert len(fb) == 3 and "502" in fb[0]["data"]["reason"]
    assert core.fleet.nodes["edge"]["load"] == 0


def test_offload_falls_back_on_timeout_and_empty_answer(squad_core, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spill")
    monkeypatch.setattr(fleet_mod, "offload_timeout", lambda: 0.05)
    core, llm = squad_core(n=2)
    add(core.fleet, "slow", health=COOL)

    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"answer": "late"})
    remote(monkeypatch, slow)
    out = run(core.squad("t", n=2, run_id="to"))
    assert [a["result"] for a in out["agents"]] == ["ok", "ok"]
    assert "TimeoutError" in fleet_events(core, "fleet.offload_fallback")[0]["data"]["reason"]

    remote(monkeypatch, lambda r: httpx.Response(200, json={"answer": "  "}))
    out = run(core.squad("t", n=2, run_id="empty"))
    assert [a["result"] for a in out["agents"]] == ["ok", "ok"]
    assert "empty answer" in fleet_events(core, "fleet.offload_fallback")[-1]["data"]["reason"]


def test_privileged_run_never_offloaded(squad_core, monkeypatch):
    from avrone.tools_extra import priv_grant
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spread")
    core, llm = squad_core()
    add(core.fleet, "edge", health=COOL)
    calls = remote(monkeypatch, ok_handler)
    core.fleet_privileged = "priv:shell"
    out = run(core.squad("t", n=4, run_id="priv1"))
    assert [a["result"] for a in out["agents"]] == ["ok"] * 4 and calls == []
    core.fleet_privileged = ""

    async def granted():
        with priv_grant("priv:admin"):
            return await core.squad("t", n=4, run_id="priv2")
    run(granted())
    assert calls == [] and fleet_events(core, "fleet.offload") == []


def test_offload_respects_health_hops_allowlist_and_cool_local(squad_core, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spill")
    core, llm = squad_core()
    add(core.fleet, "hot", health=HOT)
    add(core.fleet, "stale", age=5 * core.fleet.heartbeat_s, health=COOL)
    calls = remote(monkeypatch, ok_handler)
    run(core.squad("t", n=3, run_id="nohealthy"))
    assert calls == []

    add(core.fleet, "edge", health=COOL)
    core.fleet.allow = {"hot"}
    run(core.squad("t", n=3, run_id="notallowed"))
    assert calls == []
    core.fleet.allow = set()

    core.fleet_hops = core.fleet.max_hops
    run(core.squad("t", n=3, run_id="hops"))
    assert calls == []
    core.fleet_hops = 0

    cool_core, _ = squad_core(n=3, temp=40.0)  # governor not capping: nothing to offload
    add(cool_core.fleet, "edge", health=COOL)
    run(cool_core.squad("t", n=3, run_id="coolhere"))
    assert calls == []
    run(core.squad("t", n=3, run_id="works"))
    assert len(calls) == 2


def test_offload_plan_unit(fm, monkeypatch):
    assert fm.offload_plan(3) == []  # off by default
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spread")
    assert fm.offload_plan(3) == []  # no nodes
    add(fm, "b", health=WARM)
    add(fm, "a", health=COOL)
    assert fm.offload_plan(3) == ["a", "b", "a"]
    assert fm.offload_plan(0) == [] and fm.offload_plan(2, hops=fm.max_hops) == []
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD", "spill")
    assert fm.offload_plan(2) == ["a", "a"]
    fm.token = ""
    assert fm.offload_mode() == "off" and fm.offload_plan(2) == []


def test_doctor_warns_offload_with_open_allowlist(tmp_path):
    from types import SimpleNamespace as NS
    from avrone import doctor as d
    st = NS(provider="openclaw", llm_mode="mock", llm_base_url="http://127.0.0.1:18789/v1", llm_api_key="",
            data_dir=tmp_path / "data", workspace=tmp_path / "ws", runner="docker", allow_network=False,
            agents_can_shell=True)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    env = {"AVRONE_RECON": "deny", "AVRONE_HOST": "127.0.0.1", "AVRONE_FLEET_TOKEN": "f" * 32,
           "AVRONE_FLEET_OFFLOAD": "spill"}

    def names(e):
        rep = d.run_doctor(st, env=e, which=lambda _n: "/usr/bin/docker", sockets=(str(sock),))
        return rep, {c["name"]: c for c in rep["checks"]}
    rep, n = names(env)
    assert n["security.fleet_offload"]["status"] == "warn" and rep["toggles"]["fleet_offload"] == "spill"
    rep, n = names({**env, "AVRONE_FLEET_ALLOW": "edge1"})
    assert "security.fleet_offload" not in n
    rep, n = names({**env, "AVRONE_FLEET_OFFLOAD": "off"})
    assert "security.fleet_offload" not in n and rep["toggles"]["fleet_offload"] == "off"
    rep, n = names({**env, "AVRONE_FLEET_OFFLOAD": "weird"})
    assert rep["toggles"]["fleet_offload"] == "off"
