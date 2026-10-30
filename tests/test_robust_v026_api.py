"""v0.26 robustness: the HTTP surface fuzzed from outside. Every route, unsigned / fleet-signed /
admin, fed junk bodies and junk query params, never answers 5xx except a real upstream failure.
Found: a signed peer's malformed /api/fleet/message leaked KeyError as 502; admin /api/terminal with
a NUL byte in cmd was a 500."""
import json

import pytest
from fastapi.testclient import TestClient

from avrone.fleet import sign

ADMIN = "adm-token"
JUNK = [b"", b"{", b"null", b"[]", b'"s"', b"123", b'{"task": null}',
        b'{"task": ["x"], "mode": 5, "node": {}, "n": "x", "hops": "a"}',
        json.dumps({k: {"a": [1]} for k in ("task", "mode", "node", "agent", "message", "name", "keys", "entries",
                                            "url", "node_id", "ns", "cmd", "prompt", "checkpoint_id", "rounds",
                                            "route")}).encode(),
        json.dumps({k: "\x00" * 500 for k in ("task", "mode", "node", "agent", "message", "name", "node_id", "url",
                                              "cmd", "prompt", "checkpoint_id")}).encode(),
        json.dumps({"task": "hi", "mode": "squad", "n": 10 ** 20, "rounds": -1, "squad_size": 999999,
                    "hops": -5}).encode()]
POSTS = ["/api/fleet/agentpack", "/api/fleet/memory", "/api/fleet/message", "/api/fleet/register",
         "/api/fleet/run", "/api/fleet/tell", "/api/terminal", "/api/chat"]
GETS = ["/api/checkpoints", "/api/checkpoints/..%2F..", "/api/evolve", "/api/extensions", "/api/fleet",
        "/api/fleet/directory", "/api/fleet/jobs?limit=-1", "/api/fleet/jobs?limit=abc", "/api/gateway",
        "/api/map?window=abc", "/api/map?window=-1&min_weight=x", "/api/osint/metrics", "/api/runs?limit=-5",
        "/api/runs/..", "/api/runs/%00", "/api/runs/x/bridge", "/api/runs/..%2F..%2Fetc/bridge", "/api/status",
        "/api/telemetry?window=1e999", "/api/telemetry?window=nan", "/api/tools"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "fleet-secret")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr(server.settings, "admin_token", ADMIN, raising=False)
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


def test_get_routes_never_5xx(client):
    bad = []
    for g in GETS:
        for h in ({}, {"x-admin-token": ADMIN}):
            r = client.get(g, headers=h)
            if r.status_code >= 500:
                bad.append((g, r.status_code, r.text[:80]))
    r = client.delete("/api/checkpoints/..%2F..", headers={"x-admin-token": ADMIN})
    assert r.status_code < 500
    assert bad == []


@pytest.mark.parametrize("path", POSTS)
def test_post_routes_never_5xx(client, path, monkeypatch):
    from avrone import server

    async def no_llm(*a, **k):  # a junk body that does validate must not reach a real model
        return {"answer": "", "run_id": "x"}
    for name in ("chat", "squad", "deep"):
        monkeypatch.setattr(server.core(), name, no_llm, raising=False)
    bad = []
    for body in JUNK:
        for mode in ("plain", "signed", "admin"):
            h = {"content-type": "application/json"}
            if mode == "signed":
                h.update(sign("fleet-secret", "POST", path, body))
            if mode == "admin":
                h["x-admin-token"] = ADMIN
            r = client.post(path, content=body, headers=h)
            if r.status_code >= 500 and not (r.status_code == 502 and "unknown node" in r.text):
                bad.append((mode, body[:30], r.status_code, r.text[:100]))
    assert bad == []


def test_signed_malformed_message_is_400(client):
    body = json.dumps({"agent": ["x"], "message": "m"}).encode()
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", "/api/fleet/message", body)}
    r = client.post("/api/fleet/message", content=body, headers=h)
    assert r.status_code == 400 and "agent" in r.json()["detail"]
    body = json.dumps({"agent": "A", "message": "m", "route": "hub"}).encode()
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", "/api/fleet/message", body)}
    assert client.post("/api/fleet/message", content=body, headers=h).status_code == 400


def test_terminal_nul_byte_is_a_result(client):
    r = client.post("/api/terminal", json={"cmd": "echo a\x00b"}, headers={"x-admin-token": ADMIN})
    assert r.status_code == 200 and r.json()["exit_code"] == -1 and "NUL" in r.json()["stderr"]
