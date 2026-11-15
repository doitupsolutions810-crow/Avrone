"""Deleting an authoritative journal invalidates a long-lived reader snapshot."""
from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace

from avrone import bridge as bm


def test_long_lived_reader_forgets_deleted_journal(tmp_path):
    bridge = bm.Bridge("gone", tmp_path)
    old = bridge.post("agent", "finding", "used-to-exist")
    assert bridge.get(old["id"]) is not None
    bridge.path.unlink()
    assert bridge.posts() == []
    assert bridge.get(old["id"]) is None
    assert len(bridge) == 0


def test_deleted_journal_clears_obsolete_tombstones_before_new_admission(tmp_path):
    bridge = bm.Bridge("deleted-tombstones", tmp_path, limit=1)
    old = bridge.post("writer", "finding", "old")
    bridge.post("writer", "finding", "new")
    assert old["id"] in bridge._evicted
    bridge.path.unlink()
    assert bridge.posts() == []
    assert old["id"] not in bridge._evicted
    stats = bridge.merge_remote([dict(old)], "peer")
    assert stats["merged"] == 1
    restored = bm.Bridge("deleted-tombstones", tmp_path, limit=1)
    assert [p["id"] for p in restored.posts()] == [old["id"]]


def test_normal_post_recreates_deleted_journal_from_clean_state(tmp_path):
    bridge = bm.Bridge("recreate", tmp_path)
    previous = bridge.post("old", "finding", "previous")
    bridge.path.unlink()
    new = bridge.post("new", "finding", "new-only")
    ids = [p["id"] for p in bm.Bridge("recreate", tmp_path).posts()]
    assert ids == [new["id"]]
    assert previous["id"] not in ids


def test_read_permission_error_preserves_previous_snapshot(tmp_path, monkeypatch):
    bridge = bm.Bridge("temporary-io", tmp_path)
    old = bridge.post("writer", "finding", "cached")
    extra = {"id": "new-external", "run_id": "temporary-io",
             "kind": "finding", "text": "external", "agent": "peer",
             "refs": [], "ts": 5}
    with bridge.path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(extra) + "\n")

    real_regular_fd = bm._regular_fd
    def no_read(path, flags):
        if path == bridge.path and flags == os.O_RDONLY:
            raise PermissionError(errno.EACCES, "temporary journal read failure")
        return real_regular_fd(path, flags)
    monkeypatch.setattr(bm, "_regular_fd", no_read)
    assert [p["id"] for p in bridge.posts()] == [old["id"]]
    monkeypatch.setattr(bm, "_regular_fd", real_regular_fd)
    assert [p["id"] for p in bridge.posts()] == [old["id"], "new-external"]


def test_global_blackboard_removes_deleted_run_from_cached_core(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    post = {"id": "stored-once", "run_id": "cached-run",
            "kind": "finding", "text": "initial", "agent": "worker",
            "ts": 1}
    assert bm.blackboard_accept(core, post)[0] is False
    assert [p["id"] for p in bm.blackboard_posts(core)] == ["stored-once"]
    bm.for_core(core, "cached-run").path.unlink()
    assert bm.blackboard_posts(core) == []
    assert bm.blackboard_posts(SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path))) == []


def test_in_memory_bridge_unaffected_by_disk_absence():
    bridge = bm.Bridge("only-memory", data_dir=None)
    posted = bridge.post("agent", "finding", "available")
    assert bridge.get(posted["id"])["text"] == "available"
    assert bridge.posts()[0]["id"] == posted["id"]
