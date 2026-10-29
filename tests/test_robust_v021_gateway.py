"""v0.21 robustness: check-then-act (TOCTOU) hammer tests for the fleet gateway and agent homes.

N workers are released together through a Barrier (threads, with a tiny switch interval) or an
asyncio.gather, and each race is repeated many times. Rates measured before the fixes:

  * fleet FleetManager.verify: prune / "nonce seen?" / insert were separate steps, so the SAME
    signed request sent 8x concurrently was accepted more than once ........ 125/200 -> 0/200
    and distinct concurrent nonces were dropped from the cache (replayable later) 126/200 -> 0/200
  * fleet FleetManager.register: "already online at another url?" check and the write were
    separate, so two urls registered one node id ................................ 3/200 -> 0/200
    and readers (list_nodes/health_table/select_nodes) iterating nodes while a register inserted
    raised "dictionary changed size during iteration" ......................... 199/200 -> 0/200
    (nodes is now copy-on-write; fleet.json is written via temp + os.replace, never torn)
  * fleet FleetManager.migrate_agent: the "in flight?" check and add were separate bytecodes, so
    two threads/event loops could both migrate one agent ......................... 1/200 -> 0/200
  * agentcontainer AgentContainerPool.reap_idle: the stale decision was under the lock but the
    `docker rm` ran after it, so a concurrent ensure() re-adopted the container and handed it out
    just before it was removed ................................................. 200/200 -> 0/200
Proof kept: the privileged ("root") tier stays deny-by-default under a concurrent burst of validly
signed privileged requests (no fleet.privileged.accept, every one refused privileged_disabled).
"""
import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.agentcontainer import AgentContainerPool
from avrone.fleet import FleetManager, GatewayRefused, sign

PATH = "/api/fleet/run"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
              "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PRIVILEGED_MAX_HOPS",
              "AVRONE_AGENT_MIGRATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {}
    return c


def hammer(n, fn):
    barrier = threading.Barrier(n)
    out = [None] * n

    def work(i):
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as e:  # noqa: BLE001 - the test inspects it
            out[i] = e
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


# ------------------------------------------------------------------ nonce cache
def test_same_signed_request_is_accepted_exactly_once_under_concurrency(core, tmp_path):
    fm = FleetManager(core, tmp_path)
    body = b'{"route": [], "mode": "chat", "prompt": "hi", "hops": 1, "from": "router"}'
    for it in range(200):
        h = sign("tok", "POST", PATH, body)
        res = hammer(8, lambda i: fm.verify("POST", PATH, body, dict(h)))
        accepted = [r for r in res if r is None]
        assert len(accepted) == 1, f"iteration {it}: replay accepted {len(accepted)}x"
        assert all(isinstance(r, GatewayRefused) and r.code == "replay" for r in res if r is not None)


def test_concurrent_distinct_nonces_are_all_remembered(core, tmp_path):
    fm = FleetManager(core, tmp_path)
    body = b"{}"
    for it in range(200):
        hs = [sign("tok", "POST", PATH, body) for _ in range(8)]
        res = hammer(8, lambda i: fm.verify("POST", PATH, body, hs[i]))
        assert res == [None] * 8
        missing = [h["x-avrone-nonce"] for h in hs if h["x-avrone-nonce"] not in fm._nonces]
        assert not missing, f"iteration {it}: nonce dropped from replay cache -> replayable"
        # ...and therefore every one of them is refused as a replay afterwards
        assert all(isinstance(r, GatewayRefused) for r in
                   (lambda: [_try(fm.verify, "POST", PATH, body, h) for h in hs])())


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return e


# ------------------------------------------------------------------ privileged tier (proof)
def test_privileged_tier_stays_deny_by_default_under_a_concurrent_burst(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "router")
    fm = FleetManager(core, tmp_path)
    assert fm.privileged is False
    payload = {"route": [], "mode": "chat", "prompt": "uptime", "hops": 1, "from": "router",
               "privileged": True, "capability": "priv:shell"}
    body = json.dumps(payload).encode()
    for it in range(50):
        hs = [sign("tok", "POST", PATH, body) for _ in range(8)]
        res = hammer(8, lambda i: fm.dispatch_gateway("run", "POST", PATH, body, hs[i]))
        assert all(isinstance(r, GatewayRefused) and r.code == "privileged_disabled" for r in res), res
    accepts = [c for c in core.codex.emit.call_args_list if c.args[1] == "fleet.privileged.accept"]
    assert accepts == []


# ------------------------------------------------------------------ node registry
def test_register_same_node_id_from_two_urls_only_one_wins(core, tmp_path):
    fm = FleetManager(core, tmp_path)
    for it in range(200):
        nid = f"kid{it}"
        res = hammer(8, lambda i: fm.register({"node_id": nid, "url": f"http://h{i}.test"}))
        ok = [r for r in res if isinstance(r, dict)]
        assert len(ok) == 1, f"iteration {it}: {len(ok)} urls registered one node id"
        assert all(isinstance(r, ValueError) and "already online" in str(r) for r in res if not isinstance(r, dict))
    saved = json.loads(fm.path.read_text())
    assert len(saved["nodes"]) == 200 and saved["node_id"] == "edge"
    assert not list(tmp_path.glob(".fleet.json.*.tmp"))
    again = FleetManager(core, tmp_path)  # a restart reads the registry back whole
    assert sorted(again.nodes) == sorted(fm.nodes)


def test_register_while_readers_iterate_nodes(core, tmp_path):
    fm = FleetManager(core, tmp_path)
    for it in range(60):
        def work(i):
            if i % 2:
                return fm.register({"node_id": f"n{it}-{i}", "url": f"http://n{i}.test"})
            return [fm.list_nodes(), fm.health_table(), fm.select_nodes(3)]
        res = hammer(8, work)
        assert not [r for r in res if isinstance(r, Exception)], res
    assert len(fm.nodes) == 60 * 4


# ------------------------------------------------------------------ outbound migration guard
def test_one_migration_in_flight_per_agent_across_threads(core, tmp_path):
    fm = FleetManager(core, tmp_path)
    inside = []
    live = {"now": 0, "max": 0}
    guard = threading.Lock()

    async def fake(aid, target):
        inside.append(aid)
        with guard:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.005)
        with guard:
            live["now"] -= 1
        return {"ok": True, "agent_id": aid}
    fm._migrate_agent = fake
    for it in range(40):
        inside.clear()
        res = hammer(6, lambda i: asyncio.run(fm.migrate_agent("alpha")))
        ok = [r for r in res if isinstance(r, dict)]
        assert len(ok) >= 1 and len(inside) == len(ok)
        assert all(isinstance(r, PermissionError) for r in res if not isinstance(r, dict))
        assert fm._migrating == set()
    # never two at the same time. Counting successes was flaky: a thread whose event loop started
    # after the first migration finished may legitimately migrate again (sequential, not concurrent).
    assert live["max"] == 1, f"{live['max']} concurrent migrations of one agent"


# ------------------------------------------------------------------ agent home containers
class FakeDocker:
    def __init__(self):
        self.running: set[str] = set()

    async def __call__(self, argv, timeout):
        await asyncio.sleep(0)
        name = argv[-1]
        if argv[1] == "inspect":
            up = name in self.running
            return SimpleNamespace(exit_code=0 if up else 1, stdout="true" if up else "", stderr="")
        if argv[1] == "rm":
            await asyncio.sleep(0)
            self.running.discard(name)
        elif argv[1] == "run":
            self.running.add(argv[argv.index("--name") + 1])
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


def test_reaper_never_removes_a_container_ensure_just_handed_out(tmp_path):
    async def one(it):
        docker, now = FakeDocker(), [0.0]
        pool = AgentContainerPool(docker, "img", False, node="n", clock=lambda: now[0], idle=10)
        a, b = tmp_path / f"{it}" / "alpha", tmp_path / f"{it}" / "beta"
        a.mkdir(parents=True)
        b.mkdir()
        name_a = await pool.ensure(a)
        now[0] = 100.0  # alpha is now idle; beta's ensure reaps it while alpha's ensure runs
        got = await asyncio.gather(pool.ensure(b), pool.ensure(a), pool.reap_idle())
        assert got[1] == name_a
        assert name_a in docker.running, f"iteration {it}: handed-out container was removed"
        assert set(pool.homes) <= docker.running  # every home the pool tracks is really running
        await pool.close()
        assert docker.running == set() and pool.homes == {}

    async def main():
        for it in range(200):
            await one(it)
    asyncio.run(main())
