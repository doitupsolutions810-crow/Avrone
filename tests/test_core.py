import os

os.environ["AVRONE_LLM_MODE"] = "mock"
os.environ["AVRONE_RUNNER"] = "local"
os.environ["AVRONE_ADMIN_TOKEN"] = "test-token"

import pytest
from fastapi.testclient import TestClient

from avrone.trinity import parse_action


def test_parse_action_fenced_and_bare():
    assert parse_action('```json\n{"tool":"shell","args":{"cmd":"ls"}}\n```')["tool"] == "shell"
    assert parse_action('done: {"final": "ok"}')["final"] == "ok"
    assert parse_action("just prose") is None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_status(client):
    s = client.get("/api/status").json()
    assert s["runner"] == "local" and s["backend"] == "mock"


def test_squad_runs_parallel_agents_and_logs(client):
    r = client.post("/api/chat", json={"message": "build a todo app", "mode": "squad"}).json()
    assert r["answer"].startswith("FINAL ANSWER")
    assert len(r["agents"]) == 3
    log = client.get(f"/api/runs/{r['run_id']}").json()
    kinds = [e["kind"] for e in log]
    assert kinds[0] == "run.start" and kinds[-1] == "run.done"
    assert kinds.count("agent.spawn") == 3
    assert any(e["kind"] == "tool.result" and "avrone-online" in e["data"]["output"] for e in log)


def test_chat_mode(client):
    r = client.post("/api/chat", json={"message": "hi", "mode": "chat"}).json()
    assert "Mock worker result" in r["answer"]


def test_terminal_requires_admin(client):
    assert client.post("/api/terminal", json={"cmd": "echo hi"}).status_code == 401
    r = client.post("/api/terminal", json={"cmd": "echo hi"}, headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 200 and r.json()["stdout"].strip() == "hi"


def test_events_websocket(client):
    with client.websocket_connect("/ws/events") as ws:
        client.post("/api/chat", json={"message": "x", "mode": "chat"})
        assert ws.receive_json()["kind"] == "run.start"
