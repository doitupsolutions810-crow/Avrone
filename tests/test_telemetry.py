import os, subprocess, sys
os.environ.setdefault("AVRONE_LLM_MODE", "mock")
os.environ.setdefault("AVRONE_RUNNER", "local")
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_telemetry_and_map(client):
    client.post("/api/chat", json={"message": "map me", "mode": "squad"})
    t = client.get("/api/telemetry").json()
    assert t["runs"]["total"] >= 1 and t["runs"]["completed"] >= 1
    assert t["agents"]["spawned"] >= 3
    assert t["tools"]["shell"]["calls"] >= 1
    g = client.get("/api/map").json()
    ids = {n["id"] for n in g["nodes"]}
    assert {"core", "codex", "runner", "model:worker", "tool:shell"} <= ids
    assert any(e["to"] == "tool:shell" and e["weight"] > 0 for e in g["edges"])
    m = client.get("/api/map?format=mermaid").text
    assert m.startswith("flowchart LR") and "n_core" in m


def test_cli_map(tmp_path):
    env = {**os.environ, "AVRONE_LLM_MODE": "mock", "AVRONE_RUNNER": "local",
           "AVRONE_WORKSPACE": str(tmp_path / "ws"), "AVRONE_DATA_DIR": str(tmp_path / "d"),
           "AVRONE_PLUGINS_DIR": str(tmp_path / "np"), "AVRONE_MCP_CONFIG": str(tmp_path / "none.json")}
    subprocess.run([sys.executable, "-m", "avrone.cli", "squad", "x", "--quiet"], env=env, check=True, capture_output=True)
    out = subprocess.run([sys.executable, "-m", "avrone.cli", "map"], env=env, check=True, capture_output=True, text=True).stdout
    assert "flowchart LR" in out and "Trinity Core" in out
    out = subprocess.run([sys.executable, "-m", "avrone.cli", "telemetry"], env=env, check=True, capture_output=True, text=True).stdout
    assert '"completed": 1' in out
