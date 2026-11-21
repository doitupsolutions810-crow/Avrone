"""Long-lived Bridge readers refresh after another instance updates disk."""
from __future__ import annotations

import contextlib

from avrone import bridge as bm


def test_stale_bridge_posts_get_and_len_observe_new_writer(tmp_path):
    writer = bm.Bridge("reader-refresh", tmp_path)
    reader = bm.Bridge("reader-refresh", tmp_path)
    first = writer.post("A", "finding", "arrived-late")
    assert len(reader) == 1
    assert reader.get(first["id"])["text"] == "arrived-late"
    assert [p["id"] for p in reader.posts()] == [first["id"]]


def test_stale_reader_tracks_compaction_and_tombstones(tmp_path):
    writer = bm.Bridge("reader-compaction", tmp_path, limit=2)
    reader = bm.Bridge("reader-compaction", tmp_path, limit=2)
    ids = [writer.post("W", "finding", f"post-{i}")["id"] for i in range(8)]
    assert len(reader) == 2
    assert reader.get(ids[0]) is None
    assert [p["text"] for p in reader.posts()] == ["post-6", "post-7"]


def test_unmodified_disk_does_not_trigger_redundant_replay(tmp_path, monkeypatch):
    bridge = bm.Bridge("stable-stat", tmp_path)
    record = bridge.post("A", "finding", "already-known")
    def forbidden():
        raise AssertionError("unchanged journal must not be replayed")
    monkeypatch.setattr(bridge, "_load", forbidden)
    assert len(bridge) == 1
    assert bridge.get(record["id"]) is not None
    assert len(bridge.posts()) == 1


def test_reader_gracefully_returns_snapshot_when_sidecar_lock_unavailable(tmp_path, monkeypatch):
    writer = bm.Bridge("read-lock-failure", tmp_path)
    reader = bm.Bridge("read-lock-failure", tmp_path)
    writer.post("A", "finding", "newer-disk-only")
    @contextlib.contextmanager
    def denied():
        raise OSError("sidecar lock unavailable")
        yield
    monkeypatch.setattr(reader, "_file_lock", denied)
    assert reader.posts() == []
    assert len(reader) == 0
    assert reader.get("unknown") is None


def test_reader_refresh_after_alternating_two_writers(tmp_path):
    first = bm.Bridge("reader-interleave", tmp_path, limit=5)
    second = bm.Bridge("reader-interleave", tmp_path, limit=5)
    observer = bm.Bridge("reader-interleave", tmp_path, limit=5)
    for index in range(15):
        creator = first if index % 2 == 0 else second
        record = creator.post("worker", "finding", f"event-{index}")
        assert observer.get(record["id"])["text"] == f"event-{index}"
        assert len(observer) == min(index + 1, 5)
    assert [p["text"] for p in observer.posts()] == [
        "event-10", "event-11", "event-12", "event-13", "event-14"
    ]
