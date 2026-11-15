"""A stale Bridge instance must rebase before deciding whether an ID is unique.

The file lock must protect the validation, insertion, and append together.
Tests force collisions across separate Bridge objects and independent processes.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import contextlib
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from avrone import bridge as bm


def _post(post_id, body):
    return {"id": post_id, "kind": "finding", "agent": "worker",
            "text": body, "refs": [], "ts": 1.0}


def test_stale_peer_cannot_accept_an_existing_remote_id(tmp_path):
    earlier = bm.Bridge("same-journal", tmp_path)
    stale = bm.Bridge("same-journal", tmp_path)
    assert earlier.merge_remote([_post("same-id", "first")], "A")["merged"] == 1
    stats = stale.merge_remote([_post("same-id", "forged-second")], "B")
    assert stats == {"merged": 0, "skipped": 1, "rejected": 0}
    assert [p["text"] for p in stale.posts()] == ["first"]
    assert [p["text"] for p in bm.Bridge("same-journal", tmp_path).posts()] == ["first"]


def test_duplicate_uuid_from_two_preopened_bridges_retries(tmp_path, monkeypatch):
    earlier = bm.Bridge("two-posters", tmp_path)
    stale = bm.Bridge("two-posters", tmp_path)
    ids = iter(["a" * 12, "a" * 12, "b" * 12])
    monkeypatch.setattr(bm.uuid, "uuid4",
                        lambda: SimpleNamespace(hex=next(ids)))
    first = earlier.post("A", "finding", "one")
    second = stale.post("B", "finding", "two")
    assert (first["id"], second["id"]) == ("a" * 12, "b" * 12)
    assert [p["text"] for p in stale.posts()] == ["one", "two"]
    assert [p["text"] for p in bm.Bridge("two-posters", tmp_path).posts()] == ["one", "two"]


def test_alternating_preopened_writers_retain_latest_order(tmp_path):
    left = bm.Bridge("alternating", tmp_path, limit=3)
    right = bm.Bridge("alternating", tmp_path, limit=3)
    for i in range(40):
        writer = left if i % 2 == 0 else right
        writer.post("worker", "finding", f"record-{i}")
        assert [p["text"] for p in writer.posts()] == [
            f"record-{n}" for n in range(max(0, i - 2), i + 1)]
    expected = ["record-37", "record-38", "record-39"]
    assert [p["text"] for p in bm.Bridge("alternating", tmp_path, limit=3).posts()] == expected


def test_a_post_enters_sidecar_lock_exactly_once(tmp_path, monkeypatch):
    bridge = bm.Bridge("single-flock", tmp_path)
    real_lock = bridge._file_lock
    calls = []
    @contextlib.contextmanager
    def counted():
        calls.append("entered")
        with real_lock():
            yield
    monkeypatch.setattr(bridge, "_file_lock", counted)
    bridge.post("worker", "finding", "stored")
    assert calls == ["entered"]


def test_failing_file_lock_still_allows_memory_post(tmp_path, monkeypatch):
    bridge = bm.Bridge("failed-flock", tmp_path)
    @contextlib.contextmanager
    def denied():
        raise OSError("read-only filesystem")
        yield
    monkeypatch.setattr(bridge, "_file_lock", denied)
    posted = bridge.post("worker", "finding", "memory-only")
    assert bridge.get(posted["id"])["text"] == "memory-only"


def _competing_worker(args):
    root, index = args
    root = Path(root)
    b = bm.Bridge("cross-process-duplicate", root, limit=7)
    (root / f"prepared-{index}").touch()
    deadline = time.monotonic() + 15
    while not all((root / f"prepared-{i}").exists() for i in range(2)):
        if time.monotonic() > deadline:
            raise AssertionError("workers did not both reach pre-open barrier")
        time.sleep(.01)
    return b.merge_remote([_post("shared-remote-id", f"candidate-{index}")], f"worker-{index}")


def test_two_processes_started_from_stale_snapshots_cannot_both_accept_id(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
        stats = list(pool.map(_competing_worker, [(str(tmp_path), 0), (str(tmp_path), 1)]))
    assert sorted(s["merged"] for s in stats) == [0, 1]
    assert sorted(s["skipped"] for s in stats) == [0, 1]
    restored = bm.Bridge("cross-process-duplicate", tmp_path, limit=7)
    assert len(restored.posts()) == 1
    assert restored.posts()[0]["id"] == "shared-remote-id"
