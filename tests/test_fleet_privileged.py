"""Offline tests for the deny-by-default privileged ("root-enabled") fleet tier.

No real network: the receiving node is exercised through dispatch_gateway / handle_run directly,
outbound sends through httpx.MockTransport via fleet.HTTP_TRANSPORT, HTTP mapping via ASGITransport.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import (
    FleetManager, GatewayRefused, PRIVILEGED_CAPABILITIES,
    PRIVILEGED_SIGNATURE_HEADER, sign,
)

PATH = "/api/fleet/run"
PAIR_KEY = "test-only-pairwise-secret-not-for-deployment-0001"
ENV = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
       "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
       "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PRIVILEGED_MAX_HOPS",
       "AVRONE_PRIVILEGED_PEER_KEYS")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def make_core():
    c = MagicMock()
    c.active = {}
    c.tools.names.return_value = []
    c.runner.kind = "local"
    c.codex.emit = MagicMock()
    c.fleet_hops = 0
    c.fleet_privileged = ""
    return c


@pytest.fixture
def core():
    return make_core()


def node(core, tmp_path, monkeypatch, nid="edge", toggle=None, listed=None, **extra):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", nid)
    for k in ("AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES"):
        monkeypatch.delenv(k, raising=False)
    if toggle is not None:
        monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED", toggle)
    if listed is not None:
        monkeypatch.setenv("AVRONE_PRIVILEGED_NODES", listed)
    # Receiver maps each authorized sender to its pairwise key; the router test node
    # also maps its direct "edge" destination to the same test-only key.
    peer_keys = {}
    if listed:
        peer_keys.update({x.strip(): PAIR_KEY for x in listed.split(",") if x.strip()})
    if nid == "router":
        peer_keys.setdefault("edge", PAIR_KEY)
    monkeypatch.setenv("AVRONE_PRIVILEGED_PEER_KEYS", json.dumps(peer_keys))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    d = tmp_path / nid
    d.mkdir(exist_ok=True)
    return FleetManager(core, d)


def priv_payload(**over):
    return {"route": [], "mode": "chat", "prompt": "uptime", "hops": 1, "from": "router",
            "privileged": True, "capability": "priv:shell", **over}


def signed(payload, token="tok", pairwise_key=None):
    body = json.dumps(payload).encode()
    headers = sign(token, "POST", PATH, body)
    if pairwise_key:
        headers[PRIVILEGED_SIGNATURE_HEADER] = sign(
            pairwise_key, "POST", PATH, body, headers["x-avrone-ts"], headers["x-avrone-nonce"]
        )["x-avrone-sig"]
    return body, headers


def gate(fm, payload=None, body=None, headers=None):
    if body is None:
        sender = str((payload or {}).get("from", "")).split("/", 1)[0]
        pair_key = fm.privileged_peer_keys.get(sender) if (payload or {}).get("privileged") else None
        body, headers = signed(payload, pairwise_key=pair_key)
    return fm.dispatch_gateway("run", "POST", PATH, body, headers)


def refused(fm, payload=None, body=None, headers=None):
    with pytest.raises(GatewayRefused) as ei:
        gate(fm, payload, body, headers)
    return ei.value


def ev(core, kind):
    return [c for c in core.codex.emit.call_args_list if c.args[1] == kind]


# ---------------------------------------------------------------- deny by default
def test_deny_by_default_config(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch)
    assert fm.privileged is False and fm.privileged_nodes == set() and fm.privileged_max_hops == 1
    assert fm.info()["privileged"] is False


def test_deny_by_default_refuses_privileged_request(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch)
    e = refused(fm, priv_payload())
    assert e.code == "privileged_disabled" and "AVRONE_FLEET_PRIVILEGED" in str(e) and not e.auth
    assert ev(core, "fleet.privileged.refused")[0].kwargs["code"] == "privileged_disabled"
    assert ev(core, "fleet.refused") and not ev(core, "fleet.dispatch")


@pytest.mark.parametrize("val", ["", "1", "true", "yes", "on", "allowed", "deny", " allo w"])
def test_only_exact_allow_enables(core, tmp_path, monkeypatch, val):
    fm = node(core, tmp_path, monkeypatch, toggle=val, listed="router")
    assert fm.privileged is False
    assert refused(fm, priv_payload()).code == "privileged_disabled"


def test_toggle_on_but_no_nodes_listed_refuses(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow")
    e = refused(fm, priv_payload())
    assert e.code == "privileged_not_listed" and "AVRONE_PRIVILEGED_NODES" in str(e)


def test_refuse_when_toggle_off_even_if_listed(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="deny", listed="router")
    assert refused(fm, priv_payload()).code == "privileged_disabled"
    assert ev(core, "fleet.privileged.refused")


def test_refuse_when_node_not_listed(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="someone-else")
    e = refused(fm, priv_payload())
    assert e.code == "privileged_not_listed" and "router" in str(e)
    assert ev(core, "fleet.privileged.refused")[0].kwargs["sender"] == "router"


def test_accept_only_when_both_hold(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="ALLOW", listed="router, other")
    p = gate(fm, priv_payload())
    assert p["privileged"] is True and p["capability"] == "priv:shell"
    d = ev(core, "fleet.dispatch")[0].kwargs
    assert d["privileged"] is True and d["capability"] == "priv:shell" and d["sender"] == "router"
    assert ev(core, "fleet.privileged.accept") and not ev(core, "fleet.privileged.refused")


def test_listed_sender_still_needs_fleet_allow(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router", AVRONE_FLEET_ALLOW="parent")
    assert refused(fm, priv_payload()).code == "sender_not_allowed"


def test_privileged_hop_bound(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    assert refused(fm, priv_payload(hops=2)).code == "privileged_hops"
    fm2 = node(core, tmp_path, monkeypatch, nid="edge2", toggle="allow", listed="router",
               AVRONE_FLEET_PRIVILEGED_MAX_HOPS="99")
    assert fm2.privileged_max_hops == fm2.max_hops  # never above MAX_HOPS
    gate(fm2, priv_payload(hops=2))


# ---------------------------------------------------------------- the flag is signed
def test_privileged_flag_added_in_transit_is_rejected(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    plain = {"route": [], "mode": "chat", "prompt": "uptime", "hops": 1, "from": "router"}
    body, h = signed(plain)
    forged = json.dumps({**plain, "privileged": True, "capability": "priv:shell"}).encode()
    e = refused(fm, body=forged, headers=h)
    assert e.code == "bad_signature" and e.auth
    assert not ev(core, "fleet.dispatch") and not ev(core, "fleet.privileged.accept")


def test_privileged_capability_altered_in_transit_is_rejected(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    body, h = signed(priv_payload(capability="priv:shell"))
    forged = body.replace(b"priv:shell", b"priv:admin")
    assert refused(fm, body=forged, headers=h).code == "bad_signature"


def test_unsigned_privileged_header_is_rejected(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    body, h = signed({"route": [], "mode": "chat", "prompt": "x", "hops": 1, "from": "router"})
    e = refused(fm, body=body, headers={**h, "X-Avrone-Privileged": "1"})
    assert e.code == "privileged_unsigned"
    assert ev(core, "fleet.privileged.refused")[0].kwargs["code"] == "privileged_unsigned"


@pytest.mark.parametrize("over,code", [
    ({"privileged": False}, "privileged_capability"),            # elevated cap without the flag
    ({"privileged": "true"}, "capability"),                     # flag must be a real boolean
    ({"privileged": 1}, "capability"),
    ({"capability": "spawn:chat"}, "privileged_capability"),     # flag without an elevated cap
    ({"capability": "priv:root"}, "privileged_capability"),      # unknown elevated cap
])
def test_malformed_privileged_requests(core, tmp_path, monkeypatch, over, code):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    assert refused(fm, priv_payload(**over)).code == code


def test_privileged_never_on_message_endpoint(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    p = {"agent": "A-1", "message": "m", "from": "router", "hops": 1, "privileged": True, "capability": "priv:shell"}
    body = json.dumps(p).encode()
    h = sign("tok", "POST", "/api/fleet/message", body)
    with pytest.raises(GatewayRefused) as ei:
        fm.dispatch_gateway("message", "POST", "/api/fleet/message", body, h)
    assert ei.value.code == "privileged_capability"


# ---------------------------------------------------------------- handle_run defense in depth
def test_handle_run_regates_and_scopes_privilege(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    seen = {}

    async def chat(prompt):
        seen["priv"] = core.fleet_privileged
        return {"run_id": "r1", "answer": "ok"}
    core.chat = chat
    res = run(fm.handle_run(priv_payload()))
    assert res["answer"] == "ok" and seen["priv"] == "priv:shell" and core.fleet_privileged == ""
    off = node(core, tmp_path, monkeypatch, nid="edge3")
    with pytest.raises(GatewayRefused, match="privileged tier disabled"):
        run(off.handle_run(priv_payload()))


def test_unprivileged_run_has_no_privilege(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    seen = {}

    async def chat(prompt):
        seen["priv"] = core.fleet_privileged
        return {"answer": "ok"}
    core.chat = chat
    run(fm.handle_run({"mode": "chat", "prompt": "p", "hops": 1, "from": "router", "privileged": False}))
    assert seen["priv"] == ""


# ---------------------------------------------------------------- outbound (originating node)
def add_node(fm, nid, url):
    fm.nodes[nid] = {"node_id": nid, "url": url, "last_seen": time.time(), "load": 0,
                     "capabilities": {}, "children": []}


def test_outbound_privileged_denied_by_default(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, nid="router")
    add_node(fm, "edge", "http://edge.test")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(lambda r: pytest.fail("must not send")))
    with pytest.raises(GatewayRefused) as ei:
        run(fm.dispatch(["edge"], "chat", "x", 0, privileged="priv:shell"))
    assert ei.value.code == "privileged_disabled"
    assert ev(core, "fleet.privileged.refused")[0].kwargs["direction"] == "out"


def test_end_to_end_signed_privileged_dispatch(tmp_path, monkeypatch):
    rc, ec = make_core(), make_core()
    sender = node(rc, tmp_path, monkeypatch, nid="router", toggle="allow")
    add_node(sender, "edge", "http://edge.test")
    target = node(ec, tmp_path, monkeypatch, nid="edge", toggle="allow", listed="router")
    seen = {}

    def handler(r):
        p = target.dispatch_gateway("run", "POST", r.url.path, r.content, dict(r.headers))
        seen["p"] = p
        return httpx.Response(200, json={"path": ["edge"], "answer": "done"})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    assert run(sender.dispatch(["edge"], "chat", "x", 0, privileged="priv:admin"))["answer"] == "done"
    assert seen["p"]["privileged"] is True and seen["p"]["capability"] == "priv:admin"
    assert ev(ec, "fleet.privileged.accept")
    # same request, target node no longer lists the sender -> refused at the target
    target.privileged_nodes = set()
    with pytest.raises(GatewayRefused):
        run(sender.dispatch(["edge"], "chat", "x", 0, privileged="priv:admin"))
    assert ev(ec, "fleet.privileged.refused")[-1].kwargs["code"] == "privileged_not_listed"


def test_outbound_privileged_needs_explicit_target_and_hop_room(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, nid="router", toggle="allow")
    with pytest.raises(GatewayRefused) as ei:
        run(fm.dispatch([], "chat", "x", 0, privileged="priv:shell"))
    assert ei.value.code == "privileged_target"
    with pytest.raises(GatewayRefused) as ei:
        run(fm.dispatch(["edge"], "chat", "x", 1, privileged="priv:shell"))  # would be hop 2 > 1
    assert ei.value.code == "privileged_hops"


def test_remote_spawn_tool_reports_refusal(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, nid="router")
    core.fleet = fm
    ctx = SimpleNamespace(core=core, name="Lead-1", run_id="r")
    out = run(fleet_mod.t_remote_spawn(ctx, {"task": "t", "node": "edge", "privileged": "shell"}))
    assert out.startswith("ERROR: privileged request refused") and "disabled" in out


def test_capability_set():
    assert PRIVILEGED_CAPABILITIES == {"priv:shell", "priv:admin"}


# ---------------------------------------------------------------- HTTP status mapping
def test_server_privileged_refusal_is_403(core, tmp_path, monkeypatch):
    from avrone import server
    fm = node(core, tmp_path, monkeypatch)
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=fm))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t") as c:
            body, h = signed(priv_payload())
            r1 = await c.post(PATH, content=body, headers=h)
            body, h = signed({"mode": "chat", "prompt": "x", "hops": 1, "from": "router"})
            r2 = await c.post(PATH, content=body, headers={**h, "x-avrone-privileged": "1"})
            return r1.status_code, r1.json()["detail"], r2.status_code
    s1, detail, s2 = run(go())
    assert s1 == 403 and "privileged tier disabled" in detail and s2 == 401


# ---------------------------------------------------------------- per-peer second factor
def test_privileged_request_requires_pairwise_signature_even_with_shared_hmac(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    body, headers = signed(priv_payload())  # valid shared token, deliberately no peer signature
    e = refused(fm, body=body, headers=headers)
    assert e.code == "privileged_peer_signature" and e.auth
    assert ev(core, "fleet.privileged.refused")[-1].kwargs["code"] == "privileged_peer_signature"
    assert not ev(core, "fleet.privileged.accept")


def test_privileged_request_rejects_signature_from_wrong_peer_key(core, tmp_path, monkeypatch):
    fm = node(core, tmp_path, monkeypatch, toggle="allow", listed="router")
    body, headers = signed(priv_payload(), pairwise_key="different-peer-secret-not-the-configured-one-000")
    e = refused(fm, body=body, headers=headers)
    assert e.code == "privileged_peer_signature" and e.auth
    assert not ev(core, "fleet.dispatch")


def test_peer_key_parser_rejects_short_shared_and_invalid_peer_keys(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "shared-token")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setenv(
        "AVRONE_PRIVILEGED_PEER_KEYS",
        json.dumps({
            "router": "short",
            "bad/peer": "x" * 40,
            "same": "shared-token",
            "valid-peer": "v" * 40,
            "non-string": 42,
        }),
    )
    fm = FleetManager(core, tmp_path / "peer-parser")
    assert fm.privileged_peer_keys == {"valid-peer": "v" * 40}
    assert "v" * 40 not in json.dumps(fm.info())


def test_outbound_privileged_requires_peer_key_before_network(core, tmp_path, monkeypatch):
    fm = node(
        core, tmp_path, monkeypatch, nid="router", toggle="allow",
        AVRONE_PRIVILEGED_PEER_KEYS="{}",
    )
    add_node(fm, "edge", "http://edge.test")
    monkeypatch.setattr(
        fleet_mod, "HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: pytest.fail("request must not reach the network")),
    )
    with pytest.raises(GatewayRefused) as ei:
        run(fm.dispatch(["edge"], "chat", "x", 0, privileged="priv:shell"))
    assert ei.value.code == "privileged_peer_key_missing"
