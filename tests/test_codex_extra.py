import pytest
from pathlib import Path
from avrone.codex import Codex

def test_codex_subscribe_emit_unsubscribe(tmp_path):
    codex = Codex(tmp_path)
    q = codex.subscribe()
    ev = codex.emit("test-run", "test.kind", "test-agent", key="value", number=42)
    received = q.get_nowait()
    assert received.run_id == "test-run"
    assert received.kind == "test.kind"
    assert received.agent == "test-agent"
    assert received.data["key"] == "value"
    assert received.data["number"] == 42
    codex.unsubscribe(q)

def test_codex_replay_runs_corrupt(tmp_path):
    codex = Codex(tmp_path)
    assert codex.replay("missing-run") == []
    codex.emit("good-run", "run.start", "lead", prompt="edge-case")
    runs = codex.runs(limit=5)
    assert len(runs) >= 1 and any(r["run_id"] == "good-run" for r in runs)
    # corrupt file should be skipped by runs()
    corrupt = Path(tmp_path) / "runs" / "corrupt.jsonl"
    corrupt.write_text("this is not json\n")
    runs2 = codex.runs()
    assert any(r["run_id"] == "good-run" for r in runs2)
    replay = codex.replay("good-run")
    assert len(replay) == 1 and replay[0]["data"]["prompt"] == "edge-case"
