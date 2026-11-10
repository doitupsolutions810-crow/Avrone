"""Round 3 Codex edges: NaN/surrogates, path-traversal run_id, replay of poisoned logs."""
from __future__ import annotations

import json
import math

from avrone.codex import Codex


def test_emit_sanitizes_nan_and_lone_surrogate(tmp_path):
    c = Codex(tmp_path)
    ev = c.emit("fresh", "run.start", "T", prompt="hi\ud800there", value=float("nan"),
                also=float("inf"))
    path = c.log_dir / "fresh.jsonl"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    rec = json.loads(text)
    # Strict re-encode must succeed (this is what /api replay does).
    json.dumps(rec, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert rec["data"]["value"] is None
    assert rec["data"]["also"] is None
    assert "\ufffd" in rec["data"]["prompt"]
    assert ev.data["value"] is None


def test_emit_refuses_path_traversal_run_id(tmp_path):
    c = Codex(tmp_path)
    escaped = (c.log_dir / "../codex-escape.jsonl").resolve()
    if escaped.exists():
        escaped.unlink()
    ev = c.emit("../codex-escape", "run.start", "T", prompt="nope")
    assert ev is not None
    assert c.emitted == 1
    assert not escaped.exists(), "emit must not write outside the log dir"
    assert list(c.log_dir.glob("*.jsonl")) == []
    assert not (tmp_path / "codex-escape.jsonl").exists()


def test_replay_and_runs_sanitize_legacy_nan_logs(tmp_path):
    c = Codex(tmp_path)
    poison = {
        "run_id": "poison",
        "kind": "run.start",
        "agent": "T",
        "data": {"prompt": "pre\ud800post", "score": float("nan")},
        "ts": float("nan"),
        "id": "e1",
    }
    # Legacy log written by an older Codex (allow_nan default).
    (c.log_dir / "poison.jsonl").write_text(json.dumps(poison) + "\n", encoding="utf-8")
    evs = c.replay("poison")
    assert len(evs) == 1
    json.dumps(evs, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert evs[0]["ts"] is None or (isinstance(evs[0]["ts"], float) and math.isfinite(evs[0]["ts"]))
    assert evs[0]["data"]["score"] is None
    assert "\ufffd" in evs[0]["data"]["prompt"]

    listing = c.runs()
    assert listing
    json.dumps(listing, ensure_ascii=False, allow_nan=False).encode("utf-8")
    row = next(r for r in listing if r["run_id"] == "poison")
    assert row["started"] is None or (isinstance(row["started"], float) and math.isfinite(row["started"]))
    assert "\ufffd" in row["prompt"]


def test_emit_slash_run_id_does_not_create_subdirs(tmp_path):
    c = Codex(tmp_path)
    c.emit("nested/run", "run.start", "T", prompt="x")
    assert not (c.log_dir / "nested").exists()
    assert not (c.log_dir / "nested" / "run.jsonl").exists()
