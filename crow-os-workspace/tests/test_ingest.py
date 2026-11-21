"""Ingest reads git objects and refuses to invent GitHub rows."""

import hashlib
from pathlib import Path

from crow_os.status import propagation_graph


def test_ingest_real_hashes_syntax_and_missing_token(workspace, repo: Path):
    summary = workspace.ingest(token="")
    assert summary["github"]["state"] == "missing_token"
    assert summary["counts"]["pulls"] == 0
    assert summary["counts"]["checks"] == 0

    branches = {row["name"] for row in workspace.branches()}
    assert "main" in branches

    evidence = repo / "evidence" / "sample-evidence.json"
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    artifacts = workspace.artifacts(ref="main", kind="evidence")
    match = next(row for row in artifacts if row["path"] == "evidence/sample-evidence.json")
    assert match["sha256"] == digest
    assert match["size"] == evidence.stat().st_size

    broken = next(row for row in workspace.suites(ref="main") if row["path"].endswith("test_edge_broken.py"))
    assert broken["syntax_ok"] is False
    assert "line 1" in broken["syntax_error"]
    ok = next(row for row in workspace.suites(ref="main") if row["path"].endswith("test_ok.py"))
    assert ok["syntax_ok"] is True
    assert ok["case_count"] == 1
    assert ok["sha256"] == hashlib.sha256((repo / "tests" / "test_ok.py").read_bytes()).hexdigest()

    text = workspace.artifact_text("main", "evidence/sample-evidence.json")
    assert text["sha256"] == digest
    assert "fixture" in text["text"]

    first = workspace.store.query_one("SELECT id, payload FROM events ORDER BY id ASC LIMIT 1")
    count = workspace.store.query_one("SELECT COUNT(*) AS n FROM events")["n"]
    workspace.ingest(token="")
    again = workspace.store.query_one("SELECT payload FROM events WHERE id=?", (first["id"],))
    assert again["payload"] == first["payload"]
    assert workspace.store.query_one("SELECT COUNT(*) AS n FROM events")["n"] > count

    graph = workspace.propagation()
    assert any("test_edge_broken.py" in node["label"] for node in graph["nodes"])
    assert any(edge["reason"] == "blocks" for edge in graph["edges"])
    cleared = propagation_graph(changes=[{"id": "c", "title": "c"}], syntax_failures=[], fixes=[
        {"id": "fix-1", "status": "cleared", "change_id": "c", "file_path": "tests/a.py", "case_name": "test_a"}
    ])
    assert cleared["edges"] == [] or all(edge["reason"] == "tracks" for edge in cleared["edges"])
