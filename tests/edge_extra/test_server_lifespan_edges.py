"""Round 5 HUD remaining routes + lifespan start/stop."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from avrone import server

AUTH = {"X-Admin-Token": "test-token"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_status_ignores_adversarial_query_and_stays_200(client):
    r = client.get("/api/status", params={"q": "../" * 50 + "etc", "x": "\x00", "hours": "nan"})
    assert r.status_code == 200
    body = r.json()
    assert "runner" in body and "boot" in body and "tools" in body
    json.dumps(body, default=str, allow_nan=False)


def test_tools_and_evolve_get_are_stable(client):
    tools = client.get("/api/tools")
    assert tools.status_code == 200
    assert isinstance(tools.json(), list)
    ev = client.get("/api/evolve", headers=AUTH)
    assert ev.status_code < 500
    json.dumps(ev.json() if ev.headers.get("content-type", "").startswith("application/json") else {},
               default=str, allow_nan=False)


def test_checkpoints_list_and_delete_junk_ids(client):
    listed = client.get("/api/checkpoints", headers=AUTH)
    assert listed.status_code in {200, 403, 401}
    if listed.status_code == 200:
        body = listed.json()
        assert isinstance(body, (list, dict))
    for ck in ("", " ", "a" * 300, "../x", "id with spaces"):
        d = client.delete(f"/api/checkpoints/{ck}", headers=AUTH)
        assert d.status_code < 500, (ck, d.status_code, d.text)
        g = client.get(f"/api/checkpoints/{ck}", headers=AUTH)
        assert g.status_code < 500


def test_lifespan_start_stop_start_works(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    boots = []
    for _ in range(2):
        with TestClient(server.app) as c:
            r = c.get("/api/status")
            assert r.status_code == 200
            boots.append(r.json()["boot"])
            t = c.get("/api/tools")
            assert t.status_code == 200
    # Sequential restart must come back healthy; boot id is process-stable.
    assert boots[0] == boots[1]
