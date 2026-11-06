"""Adversarial blackboard framing and durable fan-in across independent cores."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from avrone import bridge, server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as http:
        yield http


@pytest.mark.parametrize("raw", [
    b'{"text":"a","text":"b"}', b'{"text":"a","ts":NaN}',
    b'{"text":"a","ts":1e999}', b'{"text":"\\ud800"}',
    b'{"text":"a","agent":"\\udfff"}', b'{"text":"a","id":"line\\n"}',
])
def test_bad_blackboard_frames_cannot_persist(client, raw):
    result = client.post("/api/blackboard", content=raw,
                         headers={"X-Admin-Token": "test-token"})
    assert result.status_code == 400
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "test-token"}).json()["posts"] == []


def make_core(path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=path), codex=None)


def accept(core, pid, ts, run_id):
    post, err = bridge.normalize_blackboard_post({"id": pid, "text": pid, "ts": ts, "run_id": run_id})
    assert err is None
    return bridge.blackboard_accept(core, post)


def test_independent_cores_reload_order_before_acknowledging_cursor(tmp_path):
    first, second = make_core(tmp_path), make_core(tmp_path)
    accept(first, "first", 30, "a")
    accept(second, "second", 20, "b")
    accept(first, "third", 10, "c")
    assert [p["id"] for p in bridge.blackboard_posts(first)] == ["first", "second", "third"]
    assert [p["id"] for p in bridge.blackboard_posts(second, "second")] == ["third"]
    restarted = make_core(tmp_path)
    assert [p["id"] for p in bridge.blackboard_posts(restarted, "second")] == ["third"]


def test_cached_run_bridge_refreshes_another_cores_posts(tmp_path):
    first, second = make_core(tmp_path), make_core(tmp_path)
    accept(first, "one", 1, "shared")
    accept(second, "two", 2, "shared")
    assert [p["id"] for p in bridge.blackboard_posts(first)] == ["one", "two"]


def test_bridge_journal_failure_is_refused_and_can_retry(client, monkeypatch):
    original = bridge.Bridge._write

    def broken(self, records, **kwargs):
        if kwargs.get("strict"):
            raise OSError("simulated journal failure")
        return original(self, records, **kwargs)

    monkeypatch.setattr(bridge.Bridge, "_write", broken)
    headers = {"X-Admin-Token": "test-token"}
    result = client.post("/api/blackboard", json={"id": "journal", "text": "retry"}, headers=headers)
    assert result.status_code == 503
    monkeypatch.setattr(bridge.Bridge, "_write", original)
    assert client.post("/api/blackboard", json={"id": "journal", "text": "retry"}, headers=headers).status_code == 200
    assert len(client.get("/api/blackboard", headers=headers).json()["posts"]) == 1


def test_order_publication_failure_is_503_and_same_id_retry_repairs_it(client, monkeypatch):
    original = bridge._save_order

    def broken(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(bridge, "_save_order", broken)
    headers = {"X-Admin-Token": "test-token"}
    result = client.post("/api/blackboard", json={"id": "durable", "text": "must recover"}, headers=headers)
    assert result.status_code == 503
    monkeypatch.setattr(bridge, "_save_order", original)
    retried = client.post("/api/blackboard", json={"id": "durable", "text": "must recover"}, headers=headers)
    assert retried.status_code == 200
    posts = client.get("/api/blackboard", headers=headers).json()["posts"]
    assert [p["id"] for p in posts] == ["durable"]
