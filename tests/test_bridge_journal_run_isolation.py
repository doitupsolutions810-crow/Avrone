"""Journal replay must isolate run identities without breaking legacy records."""
from __future__ import annotations

import json

from avrone.bridge import Bridge


def _line(post_id, run_id, text):
    value = {"id": post_id, "kind": "finding", "text": text,
             "agent": "writer", "ts": 42.0, "refs": []}
    if run_id is not None:
        value["run_id"] = run_id
    return json.dumps(value) + "\n"


def test_foreign_journal_record_does_not_leak_into_own_run(tmp_path):
    bridge = Bridge("run-A", tmp_path)
    original = bridge.post("agent", "finding", "valid-A")
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("foreign-B", "run-B", "foreign-data"))
        stream.write(_line("foreign-C", "run-C", "also-foreign"))
    restored = Bridge("run-A", tmp_path)
    assert restored.get(original["id"])["text"] == "valid-A"
    assert restored.get("foreign-B") is None
    assert restored.get("foreign-C") is None
    assert len(restored.posts()) == 1


def test_missing_run_id_from_historical_journal_remains_readable(tmp_path):
    bridge = Bridge("legacy-run", tmp_path)
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("legacy-record", None, "legacy-persisted"))
    assert Bridge("legacy-run", tmp_path).get("legacy-record")["text"] == "legacy-persisted"


def test_empty_run_id_legacy_record_remains_readable(tmp_path):
    bridge = Bridge("empty-run-id", tmp_path)
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("empty-record", "", "empty-legacy"))
    assert Bridge("empty-run-id", tmp_path).get("empty-record")["text"] == "empty-legacy"


def test_correct_run_id_is_preserved_after_foreign_interleaving(tmp_path):
    bridge = Bridge("known-run", tmp_path)
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("good-1", "known-run", "first"))
        stream.write(_line("bad-1", "elsewhere", "contaminant"))
        stream.write(_line("good-2", "known-run", "second"))
    assert [p["id"] for p in Bridge("known-run", tmp_path).posts()] == ["good-1", "good-2"]


def test_foreign_journal_record_not_promoted_on_compaction(tmp_path):
    bridge = Bridge("compact-run", tmp_path, limit=2)
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("bad-post", "foreign", "do-not-compact"))
    for n in range(9):
        bridge.post("writer", "finding", f"own-{n}")
    restored = Bridge("compact-run", tmp_path, limit=2)
    assert restored.get("bad-post") is None
    assert [p["text"] for p in restored.posts()] == ["own-7", "own-8"]
