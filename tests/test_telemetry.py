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



def test_telemetry_skips_oversized_line_and_keeps_later_valid_event(tmp_path, monkeypatch):
    from avrone import telemetry as telemetry_module

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_LINE_BYTES", 128)
    monkeypatch.setattr(telemetry_module, "_TELEMETRY_READ_CHUNK_BYTES", 32)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_FILE_BYTES", 4096)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_TOTAL_BYTES", 4096)
    valid = {"kind": "run.start", "agent": "worker", "run_id": "after-large",
             "data": {"mode": "test"}, "ts": 10}
    (log_dir / "events.jsonl").write_bytes(
        (b"x" * 1000) + b"\n" + __import__("json").dumps(valid).encode() + b"\n"
    )

    events = list(telemetry_module._iter_events(log_dir, since=0))

    assert events == [valid]


def test_telemetry_tail_discards_partial_prefix_and_keeps_newest_record(tmp_path, monkeypatch):
    from avrone import telemetry as telemetry_module
    import json

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_FILE_BYTES", 256)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_TOTAL_BYTES", 256)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_LINE_BYTES", 128)
    monkeypatch.setattr(telemetry_module, "_TELEMETRY_READ_CHUNK_BYTES", 32)
    valid = {"kind": "run.start", "agent": "worker", "run_id": "tail",
             "data": {"mode": "tail"}, "ts": 11}
    (log_dir / "large.jsonl").write_bytes(
        (b"old-prefix-" * 100) + b"\n" + json.dumps(valid).encode() + b"\n"
    )

    events = list(telemetry_module._iter_events(log_dir, since=0))

    assert events == [valid]


def test_newer_symlink_cannot_displace_real_log_from_file_cap(tmp_path, monkeypatch):
    from avrone import telemetry as telemetry_module
    import json

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    real_event = {"kind": "run.start", "agent": "worker", "run_id": "real",
                  "data": {"mode": "real"}, "ts": 12}
    linked_event = {"kind": "run.start", "agent": "worker", "run_id": "linked",
                    "data": {"mode": "linked"}, "ts": 13}
    real_log = log_dir / "real.jsonl"
    target = tmp_path / "outside.jsonl"
    real_log.write_text(json.dumps(real_event) + "\n", encoding="utf-8")
    target.write_text(json.dumps(linked_event) + "\n", encoding="utf-8")
    link = log_dir / "newest.jsonl"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_FILES", 1)

    events = list(telemetry_module._iter_events(log_dir, since=0))

    assert events == [real_event]



def test_telemetry_enforces_one_total_byte_budget_across_files(tmp_path, monkeypatch):
    from avrone import telemetry as telemetry_module
    import json

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = log_dir / "older.jsonl"
    newer = log_dir / "newer.jsonl"
    older_event = {"kind": "run.start", "agent": "worker", "run_id": "older",
                   "data": {"mode": "older"}, "ts": 14}
    newer_event = {"kind": "run.start", "agent": "worker", "run_id": "newer",
                   "data": {"mode": "newer"}, "ts": 15}
    older.write_text(json.dumps(older_event) + "\n", encoding="utf-8")
    newer.write_text(json.dumps(newer_event) + "\n", encoding="utf-8")
    import os
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_FILES", 10)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_FILE_BYTES", 256)
    monkeypatch.setattr(telemetry_module, "MAX_TELEMETRY_TOTAL_BYTES", 120)

    events = list(telemetry_module._iter_events(log_dir, since=0))

    assert events == [newer_event]
