"""Journal replay must isolate run identities without breaking legacy records."""
from __future__ import annotations

import json

import pytest

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



@pytest.mark.parametrize("run_id", ["a/b", "..", "/tmp/evil", "space name", "é"])
def test_global_blackboard_replays_valid_unsafe_run_filename(tmp_path, run_id):
    from types import SimpleNamespace
    from avrone.bridge import blackboard_posts
    bridge = Bridge(run_id, tmp_path)
    posted = bridge.post("writer", "finding", "present after cache eviction")
    # A new core has no cached Bridge object; it must discover the journal
    # by its sanitized filename and still retain the original run identity.
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    items = blackboard_posts(core)
    assert [p["id"] for p in items] == [posted["id"]]
    assert items[0]["text"] == "present after cache eviction"


def test_global_blackboard_keeps_colliding_stems_separate(tmp_path):
    from types import SimpleNamespace
    from avrone.bridge import blackboard_posts
    left = Bridge("a/b", tmp_path)
    right = Bridge("ab", tmp_path)
    left.post("L", "finding", "one run")
    right.post("R", "finding", "different run")
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    assert {p["text"] for p in blackboard_posts(core)} == {"one run", "different run"}


def test_global_blackboard_rejects_foreign_run_journal_injection(tmp_path):
    from types import SimpleNamespace
    from avrone.bridge import blackboard_posts
    own = Bridge("my/run", tmp_path)
    own.post("writer", "finding", "mine")
    with own.path.open("a", encoding="utf-8") as stream:
        stream.write(_line("foreign-forged", "other/run", "foreign"))
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    assert [p["text"] for p in blackboard_posts(core)] == ["mine"]
