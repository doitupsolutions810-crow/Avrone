"""Offline tests for the fleet dispatch gateway (fleet.FleetManager.dispatch_gateway).

No real network: inbound requests hit the FastAPI app over an in-process httpx.ASGITransport;
outbound calls go through httpx.MockTransport via fleet.HTTP_TRANSPORT.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import CAPABILITIES, FleetManager, GatewayRefused, WINDOW_S, sign

PATH = "/api/fleet/run"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {}
    c.tools.names.return_value = []
    c.runner.kind = "local"
    c.codex.emit = MagicMock()
    c.fleet_hops = 0
    return c


@pytest.fixture
def fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "router")
    return FleetManager(core, tmp_path)


def req(payload, token="tok", path=PATH, **kw):
    body = json.dumps(payload).encode()
    return body, sign(token, "POST", path, body, **kw)


def good(**over):
    return {"route": [], "mode": "chat", "prompt": "hi", "hops": 1, "from": "router", **over}


def events(core, kind):
    return [c for c in core.codex.emit.call_args_list if c.args[1] == kind]


def refuse_code(fm, payload=None, body=None, headers=None, kind="run"):
    if body is None:
        body, headers = req(payload)
    with pytest.raises(GatewayRefused) as ei:
        fm.dispatch_gateway(kind, "POST", PATH, body, headers)
    return ei.value


# ---------------------------------------------------------------- accept path
def test_accept_records_dispatch_event(fm, core):
    body, h = req(good())
    p = fm.dispatch_gateway("run", "POST", PATH, body, h)
    assert p["from"] == "router"
    ev = events(core, "fleet.dispatch")
    assert len(ev) == 1 and ev[0].kwargs["sender"] == "router" and ev[0].kwargs["capability"] == "spawn:chat"
    assert not events(core, "fleet.refused")


def test_message_capability_accepted(fm, core):
    body, h = req({"agent": "A-1", "message": "m", "from": "router/Lead-1", "hops": 1}, path="/api/fleet/message")
    fm.dispatch_gateway("message", "POST", "/api/fleet/message", body, h)
    assert events(core, "fleet.dispatch")[0].kwargs["capability"] == "message"


# ---------------------------------------------------------------- each refusal is distinct
def test_no_token(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    f = FleetManager(core, tmp_path)
    body, h = req(good())
    e = refuse_code(f, body=body, headers=h)
    assert e.code == "no_token" and e.auth and "fleet disabled" in str(e)


def test_tampered_signature(fm, core):
    body, h = req(good())
    tampered = body.replace(b'"hi"', b'"rm -rf"')
    e = refuse_code(fm, body=tampered, headers=h)
    assert e.code == "bad_signature" and e.auth and "bad signature" in str(e)
    assert events(core, "fleet.refused")[0].kwargs["code"] == "bad_signature"
    assert not events(core, "fleet.dispatch")


def test_wrong_token_is_bad_signature(fm):
    body, h = req(good(), token="not-the-token")
    assert refuse_code(fm, body=body, headers=h).code == "bad_signature"


def test_bad_signature_checked_before_window(fm):
    """Order: HMAC (2) comes before the timestamp window (3)."""
    body, h = req(good(), token="wrong", ts=str(int(time.time()) - 10 * WINDOW_S))
    assert refuse_code(fm, body=body, headers=h).code == "bad_signature"


def test_expired_timestamp(fm, core):
    body, h = req(good(), ts=str(int(time.time()) - WINDOW_S - 5))
    e = refuse_code(fm, body=body, headers=h)
    assert e.code == "stale" and e.auth and "stale request" in str(e)
    assert events(core, "fleet.refused")[0].kwargs["code"] == "stale"


def test_malformed_timestamp(fm):
    body, h = req(good())
    h["x-avrone-ts"] = "soon"
    assert refuse_code(fm, body=body, headers=h).code == "bad_timestamp"


def test_replayed_nonce(fm, core):
    body, h = req(good())
    fm.dispatch_gateway("run", "POST", PATH, body, h)
    e = refuse_code(fm, body=body, headers=h)
    assert e.code == "replay" and e.auth and "replayed request" in str(e)
    assert [c.kwargs["code"] for c in events(core, "fleet.refused")] == ["replay"]


def test_non_allowlisted_sender(fm, core):
    e = refuse_code(fm, good(**{"from": "mallory"}))
    assert e.code == "sender_not_allowed" and not e.auth and "mallory" in str(e)
    assert events(core, "fleet.refused")[0].kwargs["sender"] == "mallory"


def test_missing_sender_with_allowlist(fm):
    p = good()
    p.pop("from")
    assert refuse_code(fm, p).code == "sender_missing"


def test_hop_limit(fm, core):
    e = refuse_code(fm, good(hops=fm.max_hops + 1))
    assert e.code == "hops" and not e.auth and "max fleet hops exceeded" in str(e)
    assert refuse_code(fm, good(hops=-5)).code == "bad_hops"
    assert refuse_code(fm, good(hops="lots")).code == "bad_hops"


def test_capability_check(fm):
    e = refuse_code(fm, good(mode="rootshell"))
    assert e.code == "capability" and "spawn:rootshell" in str(e)
    assert refuse_code(fm, good(capability="message")).code == "capability"  # message cap on a run endpoint


def test_all_refusals_are_distinct(fm):
    codes = set()
    body, h = req(good(), token="x")
    codes.add(refuse_code(fm, body=body, headers=h).code)
    body, h = req(good(), ts=str(int(time.time()) - 999))
    codes.add(refuse_code(fm, body=body, headers=h).code)
    body, h = req(good())
    fm.dispatch_gateway("run", "POST", PATH, body, h)
    codes.add(refuse_code(fm, body=body, headers=h).code)
    codes.add(refuse_code(fm, good(**{"from": "evil"})).code)
    codes.add(refuse_code(fm, good(hops=99)).code)
    codes.add(refuse_code(fm, good(mode="nope")).code)
    assert codes == {"bad_signature", "stale", "replay", "sender_not_allowed", "hops", "capability"}


def test_policy_runs_after_auth(fm, core):
    """A non-allowlisted sender with a bad signature fails auth first, never reaching policy."""
    body, h = req(good(**{"from": "evil"}), token="wrong")
    assert refuse_code(fm, body=body, headers=h).code == "bad_signature"


def test_non_object_body(fm):
    body = b"[1,2]"
    e = refuse_code(fm, body=body, headers=sign("tok", "POST", PATH, body))
    assert e.code == "bad_body"


# ---------------------------------------------------------------- AVRONE_FLEET_REQUIRE_ALLOW
def test_empty_allowlist_allows_all_by_default(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    f = FleetManager(core, tmp_path)
    assert not f.require_allow and f.allowed("anyone")
    body, h = req(good(**{"from": "anyone"}))
    f.dispatch_gateway("run", "POST", PATH, body, h)


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_require_allow_empty_list_refuses_all(core, tmp_path, monkeypatch, val):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", val)
    f = FleetManager(core, tmp_path)
    assert f.require_allow and not f.allowed("anyone")
    e = refuse_code(f, good(**{"from": "anyone"}))
    assert e.code == "sender_not_allowed" and "REQUIRE_ALLOW" in str(e)
    with pytest.raises(PermissionError):
        f.register({"node_id": "kid", "url": "http://kid"})


def test_require_allow_with_list_still_allows_listed(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_FLEET_REQUIRE_ALLOW", "1")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "router")
    f = FleetManager(core, tmp_path)
    body, h = req(good())
    f.dispatch_gateway("run", "POST", PATH, body, h)


# ---------------------------------------------------------------- outbound mirror
def add_node(fm, nid, url="http://n.test"):
    fm.nodes[nid] = {"node_id": nid, "url": url, "last_seen": time.time(), "load": 0,
                     "capabilities": {}, "children": []}


def test_outbound_dispatch_refuses_non_allowlisted_target(fm, core):
    add_node(fm, "stranger")
    with pytest.raises(GatewayRefused) as ei:
        run(fm.dispatch(["stranger"], "chat", "x", 0))
    assert ei.value.code == "target_not_allowed"
    assert events(core, "fleet.refused")[0].kwargs["direction"] == "out"


def test_outbound_unknown_mode_normalized_and_verifiable(fm, monkeypatch):
    add_node(fm, "router", url="http://router.test")
    seen = {}

    def handler(r):
        other = FleetManager.__new__(FleetManager)
        other.__dict__.update(fm.__dict__)
        other._nonces = {}
        other.allow = {"edge"}
        seen["p"] = other.dispatch_gateway("run", "POST", r.url.path, r.content, dict(r.headers))
        return httpx.Response(200, json={"path": ["router"], "answer": "ok"})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    assert run(fm.dispatch(["router"], "weird", "x", 0))["answer"] == "ok"
    assert seen["p"]["mode"] == "squad" and seen["p"]["from"] == "edge"


def test_queue_submit_refuses_before_enqueue(fm, core):
    fm.queue = MagicMock()
    ctx = SimpleNamespace(core=SimpleNamespace(fleet=fm, fleet_hops=0), name="Lead-1", run_id="r")
    out = run(fleet_mod.t_queue_submit(ctx, {"task": "t", "node": "stranger/x"}))
    assert out.startswith("ERROR: refused by fleet gateway") and not fm.queue.enqueue.called
    ctx.core.fleet_hops = fm.max_hops
    out = run(fleet_mod.t_queue_submit(ctx, {"task": "t", "node": "router"}))
    assert "max fleet hops" in out and not fm.queue.enqueue.called


def test_capability_table_is_minimal():
    assert CAPABILITIES == {"run": {"spawn:squad", "spawn:chat", "spawn:deep"}, "message": {"message"}}


# ---------------------------------------------------------------- HTTP mapping (in-process, no sockets)
def test_server_maps_refusals_to_status(fm, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=fm))

    async def go():
        t = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            out = {}
            body, h = req(good(), token="bad")
            out["sig"] = (await c.post(PATH, content=body, headers=h)).status_code
            body, h = req(good(**{"from": "evil"}))
            out["allow"] = (await c.post(PATH, content=body, headers=h)).status_code
            body, h = req(good(hops=50))
            out["hops"] = (await c.post(PATH, content=body, headers=h)).status_code
            return out
    assert run(go()) == {"sig": 401, "allow": 403, "hops": 403}
