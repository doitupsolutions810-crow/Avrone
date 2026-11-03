"""Round 30: fleet routing and failover. Covers peer-sent load values that broke select_nodes'
sort, a future-dated last_seen, a junk or zero heartbeat setting, junk saved records, auto
dispatch that never tried a second peer, a cooldown for peers that refuse connections, failover
limited to errors raised before the request reached the peer, load counter balance across
failover, non-JSON or non-object peer replies, and register() field shapes."""
import asyncio
import json
import time
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, node_load, heartbeat_env, _finite


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_MAX_NODES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {}
    c.tools.names.return_value = ["shell"]
    c.runner.kind = "local"
    c.codex.emit = MagicMock()
    c.fleet_hops = 0
    return c


@pytest.fixture
def fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    return FleetManager(core, tmp_path)


def add(fm, nid, load=0, age=0.0, score_headroom=None):
    rec = {"node_id": nid, "url": f"http://{nid}.test", "last_seen": time.time() - age, "load": load,
           "capabilities": {"runner": "local"}, "children": []}
    if score_headroom is not None:
        rec["health"] = {"state": "ok", "headroom": score_headroom}
    fm.nodes[nid] = rec
    return rec


def transport(monkeypatch, handler):
    calls = []

    def h(req):
        calls.append(req.url.host)
        return handler(req)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(h))
    return calls


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("v,exp", [(3, 3), ("7", 7), ("abc", 0), (None, 0), (-5, 0), (float("nan"), 0),
                                   (float("inf"), 0), (True, 0), (1e12, 10000), (2.9, 2), ([1], 0)])
def test_node_load(v, exp):
    assert node_load({"load": v}) == exp


def test_node_load_non_dict():
    assert node_load("x") == 0 and node_load(None) == 0


@pytest.mark.parametrize("env,exp", [("", 15.0), ("abc", 15.0), ("0", 15.0), ("-3", 15.0), ("nan", 15.0),
                                     ("0.2", 1.0), ("30", 30.0), ("99999", 3600.0), ("inf", 15.0)])
def test_heartbeat_env(monkeypatch, env, exp):
    monkeypatch.setenv("AVRONE_FLEET_HEARTBEAT", env)
    assert heartbeat_env() == exp


def test_bad_heartbeat_does_not_crash_boot(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_HEARTBEAT", "fifteen")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    f = FleetManager(core, tmp_path)
    assert f.heartbeat_s == 15.0


# ------------------------------------------------------------------ selection
def test_string_load_does_not_break_selection(fm):
    add(fm, "a", load="busy"); add(fm, "b", load=2); add(fm, "c", load=None)
    ids = [n["node_id"] for n in fm.select_nodes(3)]
    assert sorted(ids) == ["a", "b", "c"]


def test_negative_load_is_not_favoured(fm):
    add(fm, "liar", load=-1000); add(fm, "honest", load=0)
    # equal health; liar's load clamps to 0 so the tie breaks on node id
    assert [n["node_id"] for n in fm.select_nodes(2)] == ["honest", "liar"]


@pytest.mark.parametrize("k,exp", [(None, 1), ("2", 2), ("x", 1), (-1, 0), (2.7, 2)])
def test_select_nodes_k_coerced(fm, k, exp):
    for i in range(3):
        add(fm, f"n{i}")
    assert len(fm.select_nodes(k)) == exp


def test_future_last_seen_is_offline(fm):
    rec = add(fm, "time-traveler")
    rec["last_seen"] = time.time() + 3600
    assert fm.status_of(rec) == "offline"
    assert fm.select_nodes(5) == []


def test_slight_future_last_seen_still_online(fm):
    rec = add(fm, "skew")
    rec["last_seen"] = time.time() + 2
    assert fm.status_of(rec) == "online"


@pytest.mark.parametrize("ls", ["yesterday", None, float("nan"), [1]])
def test_junk_last_seen_reads_offline(fm, ls):
    rec = add(fm, "j"); rec["last_seen"] = ls
    assert fm.status_of(rec) == "offline"


def test_saved_registry_with_junk_values_loads(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    (tmp_path / "fleet.json").write_text(json.dumps({"node_id": "root", "nodes": {
        "a": {"node_id": "a", "url": "http://a.test", "last_seen": "soon", "load": "lots"},
        "b": {"node_id": "b", "url": "http://b.test", "last_seen": time.time(), "load": 4},
        "c": "not a record"}}))
    f = FleetManager(core, tmp_path)
    assert set(f.nodes) == {"a", "b"}
    assert f.nodes["a"]["load"] == 0 and f.nodes["a"]["last_seen"] == 0.0
    assert [n["node_id"] for n in f.select_nodes(5)] == ["b"]
    assert all(isinstance(r["load"], int) for r in f.health_table())


# ------------------------------------------------------------------ register
def test_register_sanitizes_fields(fm):
    fm.register({"node_id": "kid", "url": "http://kid.test", "load": "99x",
                 "capabilities": ["shell"], "children": ["a", 3, None, "b"]})
    rec = fm.nodes["kid"]
    assert rec["load"] == 0 and rec["capabilities"] == {} and rec["children"] == ["a", "b"]


def test_register_clears_cooldown(fm):
    rec = add(fm, "kid"); rec["cooldown_until"] = time.time() + 999
    fm.register({"node_id": "kid", "url": "http://kid.test"})
    assert "cooldown_until" not in fm.nodes["kid"]
    assert [n["node_id"] for n in fm.select_nodes(1)] == ["kid"]


# ------------------------------------------------------------------ failover
def test_auto_dispatch_fails_over_to_next_peer(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        if req.url.host == "best.test":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"answer": "ok", "path": ["next"]})
    calls = transport(monkeypatch, h)
    res = run(fm.dispatch([], "squad", "t", 0))
    assert res["answer"] == "ok" and calls == ["best.test", "next.test"]
    assert fm.nodes["best"]["load"] == 0 and fm.nodes["next"]["load"] == 0
    assert fm.nodes["best"]["cooldown_until"] > time.time()


def test_cooled_peer_skipped_on_next_dispatch(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)
    fail = {"on": True}

    def h(req):
        if req.url.host == "best.test" and fail["on"]:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"answer": req.url.host})
    calls = transport(monkeypatch, h)
    run(fm.dispatch([], "squad", "t", 0))
    calls.clear()
    assert run(fm.dispatch([], "squad", "t", 0))["answer"] == "next.test"
    assert calls == ["next.test"]


def test_cooldown_expires(fm, monkeypatch):
    rec = add(fm, "a"); rec["cooldown_until"] = time.time() - 1
    assert fm.select_nodes(1)[0]["node_id"] == "a"


def test_explicit_route_never_fails_over(fm, monkeypatch):
    add(fm, "kid"); add(fm, "other")

    def h(req):
        raise httpx.ConnectError("refused", request=req)
    calls = transport(monkeypatch, h)
    with pytest.raises(httpx.ConnectError):
        run(fm.dispatch(["kid"], "squad", "t", 0))
    assert calls == ["kid.test"]


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_no_failover_once_request_may_have_run(fm, monkeypatch, exc):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        raise exc("mid-flight", request=req)
    calls = transport(monkeypatch, h)
    with pytest.raises(exc):
        run(fm.dispatch([], "squad", "t", 0))
    assert calls == ["best.test"]  # the job may already be running on best: never start it twice
    assert "cooldown_until" not in fm.nodes["best"]


def test_http_5xx_does_not_fail_over(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)
    calls = transport(monkeypatch, lambda req: httpx.Response(502, text="bad gateway"))
    with pytest.raises(RuntimeError, match="502"):
        run(fm.dispatch([], "squad", "t", 0))
    assert calls == ["best.test"]


def test_failover_capped_and_reports_every_peer(fm, monkeypatch):
    for i, h in enumerate((1.0, 0.9, 0.8, 0.7, 0.6)):
        add(fm, f"n{i}", score_headroom=h)

    def h(req):
        raise httpx.ConnectError("refused", request=req)
    calls = transport(monkeypatch, h)
    with pytest.raises(RuntimeError) as ei:
        run(fm.dispatch([], "squad", "t", 0))
    assert len(calls) == fleet_mod.FAILOVER_MAX == 3
    assert "n0, n1, n2" in str(ei.value)
    assert all(fm.nodes[f"n{i}"]["load"] == 0 for i in range(5))


def test_connect_timeout_fails_over(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        if req.url.host == "best.test":
            raise httpx.ConnectTimeout("slow syn", request=req)
        return httpx.Response(200, json={"answer": "ok"})
    transport(monkeypatch, h)
    assert run(fm.dispatch([], "squad", "t", 0))["answer"] == "ok"


def test_single_peer_auto_keeps_original_error(fm, monkeypatch):
    add(fm, "only")
    transport(monkeypatch, lambda req: (_ for _ in ()).throw(httpx.ConnectError("x", request=req)))
    with pytest.raises(httpx.ConnectError):
        run(fm.dispatch([], "squad", "t", 0))
    assert fm.nodes["only"]["load"] == 0


def test_unreachable_is_audited(fm, monkeypatch, core):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        if req.url.host == "best.test":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={})
    transport(monkeypatch, h)
    run(fm.dispatch([], "squad", "t", 0))
    events = [c.args[1] for c in core.codex.emit.call_args_list]
    assert "fleet.unreachable" in events


def test_load_balanced_during_concurrent_failover(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        if req.url.host == "best.test":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"answer": "ok"})
    transport(monkeypatch, h)

    async def many():
        return await asyncio.gather(*(fm.dispatch([], "squad", "t", 0) for _ in range(6)))
    assert all(r["answer"] == "ok" for r in run(many()))
    assert fm.nodes["best"]["load"] == 0 and fm.nodes["next"]["load"] == 0


# ------------------------------------------------------------------ reply shapes
@pytest.mark.parametrize("resp,match", [
    (lambda: httpx.Response(200, text="<html>Sign in to Wi-Fi</html>", headers={"content-type": "text/html"}),
     "not JSON \\(text/html\\).*Sign in to Wi-Fi"),
    (lambda: httpx.Response(200, text=""), "not JSON"),
    (lambda: httpx.Response(200, json=["a", "b"]), "JSON list, not an object"),
    (lambda: httpx.Response(200, json="ok"), "JSON str"),
    (lambda: httpx.Response(200, text="null"), "JSON NoneType"),
])
def test_bad_reply_shapes_raise_clear_errors(fm, monkeypatch, resp, match):
    add(fm, "kid")
    transport(monkeypatch, lambda req: resp())
    with pytest.raises(RuntimeError, match=match):
        run(fm.dispatch(["kid"], "squad", "t", 0))
    assert fm.nodes["kid"]["load"] == 0


def test_parent_register_html_reply_is_clear(fm, monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200, text="<html>portal</html>"))
    with pytest.raises(RuntimeError, match="parent.test/api/fleet/register -> 200 but not JSON"):
        run(fm._post("http://parent.test", "/api/fleet/register", {"node_id": "root"}))


def test_remote_spawn_tool_reports_failover_answer(fm, monkeypatch):
    add(fm, "best", score_headroom=1.0); add(fm, "next", score_headroom=0.6)

    def h(req):
        if req.url.host == "best.test":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"answer": "from next", "path": ["next"], "run_id": "r9"})
    transport(monkeypatch, h)
    from types import SimpleNamespace
    ctx = SimpleNamespace(core=SimpleNamespace(fleet=fm, fleet_hops=0), run_id="r1")
    monkeypatch.setattr(fleet_mod, "_fleet", lambda c: fm)
    out = run(fleet_mod.t_remote_spawn(ctx, {"task": "go"}))
    assert "from next" in out and "next" in out


def test_finite_helper():
    assert _finite("1.5", 0) == 1.5 and _finite(True, 9) == 9 and _finite(float("-inf"), 2) == 2
