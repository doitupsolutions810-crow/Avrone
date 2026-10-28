"""Memory sync over the signed fleet gateway (/api/fleet/memory). Fully offline: outbound pushes
go through httpx.MockTransport (fleet.HTTP_TRANSPORT) into the receiving FleetManager's
dispatch_gateway, or into the real FastAPI app over an in-process ASGITransport."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone import memstore
from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.fleet import MEMORY_PATH, FleetManager, GatewayRefused, WINDOW_S, sign

ENVS = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
        "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
        "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_MEMORY_MAX_ENTRIES",
        "AVRONE_MEMORY_MAX_VALUE", "AVRONE_MEMORY_MAX_NAMESPACES")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def mkcore():
    c = MagicMock()
    c.active = {}
    c.codex.emit = MagicMock()
    return c


def mkfleet(monkeypatch, d, node, token="tok", parent="", allow=""):
    for k, v in (("AVRONE_FLEET_TOKEN", token), ("AVRONE_NODE_ID", node), ("AVRONE_FLEET_PARENT", parent),
                 ("AVRONE_FLEET_ALLOW", allow)):
        monkeypatch.setenv(k, v)
    return FleetManager(mkcore(), d)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    edge = mkfleet(monkeypatch, tmp_path / "edge", "edge", parent="http://router")
    router = mkfleet(monkeypatch, tmp_path / "router", "router", allow="edge")
    return edge, router


def ctx_for(fm, name="Builder-1"):
    s = Settings(workspace=fm.data_dir / "ws", data_dir=fm.data_dir)
    return SimpleNamespace(core=SimpleNamespace(settings=s, fleet=fm, codex=None), run_id="r1", name=name, depth=0)


def store(fm):
    return memstore.MemoryStore(fm.data_dir / "memory.jsonl")


def refused(fm, kind):
    return [c for c in fm.core.codex.emit.call_args_list if c.args[1] == "fleet.refused" and c.kwargs.get("code") == kind]


def transport_to(receiver, mutate=None, seen=None):
    """MockTransport standing in for the parent's HTTP server: same gateway + status mapping."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        headers = {k.lower(): v for k, v in request.headers.items()}
        if mutate:
            body, headers = mutate(body, headers)
        if seen is not None:
            seen.append((body, headers))
        try:
            payload = receiver.dispatch_gateway("memory", request.method, request.url.path, body, headers)
            return httpx.Response(200, json=receiver.receive_memory(payload))
        except GatewayRefused as e:
            return httpx.Response(401 if e.auth else 403, json={"detail": str(e)})
        except ValueError as e:
            return httpx.Response(400, json={"detail": str(e)})
    return httpx.MockTransport(handler)


def sync(fm, name="Builder-1", **args):
    return run(tx.t_memory_sync(ctx_for(fm, name), args))


# ---------------------------------------------------------------- valid push merges under the sender
def test_valid_push_merges_under_sender_namespace(pair, monkeypatch):
    edge, router = pair
    store(router).save("builder", "db", "router's own choice")  # receiver's own namespace
    store(edge).save("builder", "db", "edge found: sqlite", agent="Builder-1")
    store(edge).save("builder", "port", "8080", agent="Builder-1")
    store(edge).save("other", "secret-ish", "not mine to send", agent="Other-1")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router))
    out = sync(edge)
    assert out.startswith("synced 2 entries") and "merged=2" in out
    spaces = store(router).namespaces()
    assert spaces["builder"]["db"]["value"] == "router's own choice"  # never overwritten
    remote = spaces["fleet:edge/builder"]
    assert remote["db"]["value"] == "edge found: sqlite" and remote["db"]["source"] == "edge"
    assert set(remote) == {"db", "port"} and "fleet:edge/other" not in spaces  # scope=agent only
    merges = [c for c in router.core.codex.emit.call_args_list if c.args[1] == "fleet.memory.merge"]
    assert len(merges) == 1 and merges[0].kwargs["sender"] == "edge"
    assert [c for c in router.core.codex.emit.call_args_list if c.args[1] == "fleet.dispatch"][0].kwargs[
        "capability"] == "memory:sync"


def test_node_scope_and_last_writer_wins(pair, monkeypatch):
    edge, router = pair
    store(edge).save("builder", "db", "v1")
    store(edge).save("researcher", "lead", "x")
    store(edge).merge("fleet:leaf/builder", [{"key": "relayed", "value": "v", "ts": 1}])  # received, not local
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router))
    assert "merged=2" in sync(edge, scope="node")
    spaces = store(router).namespaces()
    assert {"fleet:edge/builder", "fleet:edge/researcher"} <= set(spaces)
    assert not any("leaf" in ns for ns in spaces)  # received memory is never re-pushed
    assert "skipped=2" in sync(edge, scope="node")  # idempotent re-push
    store(edge).save("builder", "db", "v2")
    assert "merged=1" in sync(edge, keys=["db"])
    assert store(router).load("fleet:edge/builder")["db"]["value"] == "v2"
    # an older entry never overwrites a newer one
    router.receive_memory({"from": "edge", "capability": "memory:sync",
                           "entries": [{"ns": "builder", "key": "db", "value": "stale", "ts": 1}]})
    assert store(router).load("fleet:edge/builder")["db"]["value"] == "v2"


def test_sender_cannot_target_receiver_namespaces(pair):
    edge, router = pair
    store(router).save("builder", "db", "mine")
    res = router.receive_memory({"from": "edge", "capability": "memory:sync", "entries": [
        {"ns": "fleet:router/builder", "key": "db", "value": "x", "ts": 9e12},
        {"ns": "../builder", "key": "db", "value": "x", "ts": 9e12},
        {"ns": "builder", "key": "db", "value": "edge", "ts": 9e12}, "junk"]})
    assert res["rejected"] == 3 and res["merged"] == 1 and res["namespaces"] == ["fleet:edge/builder"]
    assert store(router).load("builder")["db"]["value"] == "mine"
    with pytest.raises(ValueError):  # a sender claiming the receiver's own id
        router.receive_memory({"from": "router", "capability": "memory:sync", "entries": []})
    with pytest.raises(ValueError):
        router.receive_memory({"from": "edge", "capability": "memory:sync",
                               "entries": [{"ns": "a", "key": str(i), "value": "v", "ts": 1} for i in range(201)]})
    with pytest.raises(PermissionError):
        router.receive_memory({"from": "edge", "capability": "spawn:chat", "entries": []})


def test_receiver_caps_hold(pair, monkeypatch):
    edge, router = pair
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "8")
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    res = router.receive_memory({"from": "edge", "capability": "memory:sync", "entries": [
        {"ns": "b", "key": "big", "value": "x" * 9, "ts": 1}] + [
        {"ns": "b", "key": f"k{i}", "value": "v", "ts": i + 2} for i in range(4)]})
    assert res["rejected"] == 1 and len(store(router).load("fleet:edge/b")) == 2


# ---------------------------------------------------------------- gateway refusals
def test_tampered_push_refused(pair, monkeypatch):
    edge, router = pair
    store(edge).save("builder", "db", "honest")

    def tamper(body, headers):
        return body.replace(b"honest", b"evil!!"), headers
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router, mutate=tamper))
    out = sync(edge)
    assert out.startswith("ERROR") and "401" in out and "bad signature" in out
    assert "fleet:edge/builder" not in store(router).namespaces() and refused(router, "bad_signature")


def test_stale_push_refused(pair, monkeypatch):
    edge, router = pair
    store(edge).save("builder", "db", "v")

    def restale(body, headers):
        return body, {**headers, **sign("tok", "POST", MEMORY_PATH, body, ts=str(int(time.time()) - WINDOW_S - 5))}
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router, mutate=restale))
    out = sync(edge)
    assert out.startswith("ERROR") and "401" in out and "stale" in out
    assert not store(router).namespaces() and refused(router, "stale")


def test_replayed_push_refused(pair, monkeypatch):
    edge, router = pair
    store(edge).save("builder", "db", "v")
    seen = []
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router, seen=seen))
    assert "merged=1" in sync(edge)
    body, headers = seen[0]
    with pytest.raises(GatewayRefused) as ei:  # the captured request, byte for byte
        router.dispatch_gateway("memory", "POST", MEMORY_PATH, body, headers)
    assert ei.value.code == "replay" and refused(router, "replay")


def test_non_allowlisted_push_refused(tmp_path, monkeypatch):
    edge = mkfleet(monkeypatch, tmp_path / "edge", "edge", parent="http://router")
    router = mkfleet(monkeypatch, tmp_path / "router", "router", allow="someone-else")
    store(edge).save("builder", "db", "v")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router))
    out = sync(edge)
    assert out.startswith("ERROR") and "403" in out and "not in AVRONE_FLEET_ALLOW" in out
    assert not store(router).namespaces() and refused(router, "sender_not_allowed")


def test_require_allow_with_empty_list_refuses(tmp_path, monkeypatch):
    edge = mkfleet(monkeypatch, tmp_path / "edge", "edge", parent="http://router")
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", "1")
    router = mkfleet(monkeypatch, tmp_path / "router", "router", allow="")
    store(edge).save("builder", "db", "v")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(router))
    assert "403" in sync(edge) and not store(router).namespaces()


def test_wrong_token_capability_and_privileged_refused(pair):
    _, router = pair
    def push(payload, token="tok", extra=None):
        body = json.dumps(payload).encode()
        h = {**sign(token, "POST", MEMORY_PATH, body), **(extra or {})}
        with pytest.raises(GatewayRefused) as ei:
            router.dispatch_gateway("memory", "POST", MEMORY_PATH, body, h)
        return ei.value.code
    ok = {"from": "edge", "hops": 1, "capability": "memory:sync", "entries": []}
    assert push(ok, token="wrong") == "bad_signature"
    assert push({**ok, "capability": "spawn:chat"}) == "capability"
    assert push({k: v for k, v in ok.items() if k != "capability"}) == "capability"
    assert push({**ok, "privileged": True}) == "privileged_capability"
    assert push(ok, extra={"x-avrone-privileged": "1"}) == "privileged_unsigned"
    assert push({**ok, "hops": 99}) == "hops"
    assert not store(router).namespaces()


def test_receiver_without_token_refuses(tmp_path, monkeypatch):
    router = mkfleet(monkeypatch, tmp_path / "router", "router", token="")
    body = json.dumps({"from": "edge", "capability": "memory:sync", "entries": []}).encode()
    with pytest.raises(GatewayRefused) as ei:
        router.dispatch_gateway("memory", "POST", MEMORY_PATH, body, sign("tok", "POST", MEMORY_PATH, body))
    assert ei.value.code == "no_token"


# ---------------------------------------------------------------- fleet-off / misconfig on the sender
def test_fleet_off_returns_clear_error(tmp_path, monkeypatch):
    off = mkfleet(monkeypatch, tmp_path / "off", "edge", token="", parent="http://router")
    store(off).save("builder", "db", "v")
    assert sync(off).startswith("ERROR: fleet disabled")
    ctx = ctx_for(off)
    ctx.core.fleet = None
    assert run(tx.t_memory_sync(ctx, {})).startswith("ERROR: fleet disabled")
    with pytest.raises(GatewayRefused):
        run(off.push_memory([], "edge"))


def test_no_parent_and_bad_scope(tmp_path, monkeypatch):
    orphan = mkfleet(monkeypatch, tmp_path / "o", "edge", parent="")
    assert sync(orphan).startswith("ERROR: no parent node")
    edge = mkfleet(monkeypatch, tmp_path / "e", "edge", parent="http://router")
    assert sync(edge, scope="everything").startswith("ERROR: scope")
    assert sync(edge).startswith("nothing to sync")


def test_memory_sync_tool_registered():
    from avrone.tools import ToolRegistry
    r = ToolRegistry()
    tx.register_extra_tools(r)
    assert {"memory_save", "memory_recall", "memory_list", "memory_sync"} <= set(r.names())


# ---------------------------------------------------------------- the real HTTP endpoint
def test_server_endpoint_is_gateway_protected(pair, monkeypatch):
    from avrone import server
    edge, router = pair
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=router))

    def signed(payload, token="tok"):
        body = json.dumps(payload).encode()
        return body, {"content-type": "application/json", **sign(token, "POST", MEMORY_PATH, body)}

    async def go():
        t = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            good = {"from": "edge/Builder-1", "hops": 1, "capability": "memory:sync",
                    "entries": [{"ns": "builder", "key": "db", "value": "v", "ts": 5}]}
            out = {"unsigned": (await c.post(MEMORY_PATH, json=good)).status_code}
            b, h = signed(good, token="bad")
            out["badsig"] = (await c.post(MEMORY_PATH, content=b, headers=h)).status_code
            b, h = signed({**good, "from": "evil"})
            out["allow"] = (await c.post(MEMORY_PATH, content=b, headers=h)).status_code
            b, h = signed(good)
            r = await c.post(MEMORY_PATH, content=b, headers=h)
            out["ok"], out["merged"] = r.status_code, r.json()["merged"]
            out["replay"] = (await c.post(MEMORY_PATH, content=b, headers=h)).status_code
            b, h = signed({**good, "entries": "nope"})
            out["badbody"] = (await c.post(MEMORY_PATH, content=b, headers=h)).status_code
            return out
    assert run(go()) == {"unsigned": 401, "badsig": 401, "allow": 403, "ok": 200, "merged": 1,
                         "replay": 401, "badbody": 400}
    assert store(router).load("fleet:edge/builder")["db"]["value"] == "v"
