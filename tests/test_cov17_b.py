"""Round 17 coverage: evolve history write failure removes its temp file and never raises."""
import json

from avrone import evolve


def test_history_write_failure_cleans_tmp(tmp_path, monkeypatch):
    path = tmp_path / "h" / "history.jsonl"
    monkeypatch.setattr(evolve.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("ro")))
    evolve._record_history_at(path, {"loop_id": "L1", "goal": "g", "iterations": []})  # no raise
    assert not path.exists() and not list(path.parent.glob(".*.tmp"))


def test_history_tmp_unlink_failure_swallowed(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(evolve.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("ro")))
    monkeypatch.setattr(evolve.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("busy")))
    evolve._record_history_at(path, {"loop_id": "L2", "goal": "g", "iterations": []})
    assert not path.exists()


def test_history_roundtrip_still_works(tmp_path):
    path = tmp_path / "history.jsonl"
    evolve._record_history_at(path, {"loop_id": "L3", "goal": "g", "iterations": [{"outcome": "accepted"}]})
    row = json.loads(path.read_text().splitlines()[0])
    assert row["loop_id"] == "L3" and row["accepted_count"] == 1
