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



def test_flaky_sidecar_lock_cannot_retry_unprotected_post_to_disk(tmp_path, monkeypatch):
    """A transient lock error must not turn a stale admission into a write."""
    bridge = bm.Bridge("flaky-lock-post", tmp_path)
    real = bridge._file_lock
    calls = []
    @contextlib.contextmanager
    def flaky_lock():
        calls.append("attempt")
        if len(calls) == 1:
            raise OSError("first lock attempt denied")
        with real():
            yield
    monkeypatch.setattr(bridge, "_file_lock", flaky_lock)

    post = bridge.post("worker", "finding", "memory-only")
    assert bridge.get(post["id"])["text"] == "memory-only"
    assert calls == ["attempt"]
    assert bm.Bridge("flaky-lock-post", tmp_path).posts() == []


def test_flaky_sidecar_lock_cannot_retry_unprotected_remote_merge(tmp_path, monkeypatch):
    """Remote IDs require the same no-unprotected-write guarantee."""
    bridge = bm.Bridge("flaky-lock-merge", tmp_path)
    real = bridge._file_lock
    calls = []
    @contextlib.contextmanager
    def flaky_lock():
        calls.append("attempt")
        if len(calls) == 1:
            raise OSError("first lock attempt denied")
        with real():
            yield
    monkeypatch.setattr(bridge, "_file_lock", flaky_lock)

    stats = bridge.merge_remote([_post("remote-unsafe", "memory-only")], "remote")
    assert stats["merged"] == 1
    assert bridge.get("remote-unsafe")["text"] == "memory-only"
    assert calls == ["attempt"]
    assert bm.Bridge("flaky-lock-merge", tmp_path).posts() == []


def test_restored_sidecar_lock_persists_later_post_only(tmp_path, monkeypatch):
    """After a transient failure the next successful transaction can persist."""
    bridge = bm.Bridge("flaky-lock-recover", tmp_path)
    original = bridge._file_lock
    attempts = []
    @contextlib.contextmanager
    def once_denied():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("temporary lock error")
        with original():
            yield
    monkeypatch.setattr(bridge, "_file_lock", once_denied)

    volatile = bridge.post("worker", "finding", "volatile")
    durable = bridge.post("worker", "finding", "durable")
    assert volatile["id"] != durable["id"]
    assert len(attempts) == 2
    persisted = bm.Bridge("flaky-lock-recover", tmp_path).posts()
    assert [p["id"] for p in persisted] == [durable["id"]]



def test_lock_recovery_does_not_allow_ref_to_prior_volatile_post(tmp_path, monkeypatch):
    """After a best-effort write, durable reference validation must rebase."""
    bridge = bm.Bridge("volatile-refs", tmp_path)
    real_lock = bridge._file_lock
    count = 0

    @contextlib.contextmanager
    def transient():
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("sidecar unavailable")
        with real_lock():
            yield

    monkeypatch.setattr(bridge, "_file_lock", transient)
    volatile = bridge.post("worker", "finding", "not persisted")
    assert bridge.get(volatile["id"]) is not None
    with pytest.raises(bm.BridgeError, match="unknown post id"):
        bridge.post("worker", "answer", "must not persist dangling ref",
                    refs=[volatile["id"]])
    assert bridge.get(volatile["id"]) is None
    durable = bridge.post("worker", "finding", "persisted")
    reopened = bm.Bridge("volatile-refs", tmp_path)
    assert [p["id"] for p in reopened.posts()] == [durable["id"]]


def test_remote_merge_lock_recovery_rebases_volatile_records(tmp_path, monkeypatch):
    bridge = bm.Bridge("volatile-merge", tmp_path)
    real_lock = bridge._file_lock
    count = 0

    @contextlib.contextmanager
    def transient():
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("sidecar unavailable")
        with real_lock():
            yield

    monkeypatch.setattr(bridge, "_file_lock", transient)
    assert bridge.merge_remote([_post("volatile-id", "temporary")], "peer")["merged"] == 1
    assert bridge.get("volatile-id") is not None
    assert bridge.merge_remote([_post("durable-id", "permanent")], "peer")["merged"] == 1
    assert bridge.get("volatile-id") is None
    assert [p["id"] for p in bm.Bridge("volatile-merge", tmp_path).posts()] == ["durable-id"]


def test_failed_journal_open_after_lock_marks_post_non_durable(tmp_path, monkeypatch):
    """A lock can work while writing the journal itself fails."""
    bridge = bm.Bridge("journal-io-failure", tmp_path)
    real_fd = bm._regular_fd
    failed = False

    def one_journal_failure(path, flags):
        nonlocal failed
        if path == bridge.path and flags & bm.os.O_APPEND and not failed:
            failed = True
            raise OSError("simulated full disk")
        return real_fd(path, flags)

    monkeypatch.setattr(bm, "_regular_fd", one_journal_failure)
    volatile = bridge.post("worker", "finding", "failed-to-write")
    assert failed
    assert bridge.get(volatile["id"]) is not None
    durable = bridge.post("worker", "finding", "written-later")
    assert [p["id"] for p in bm.Bridge("journal-io-failure", tmp_path).posts()] == [durable["id"]]
    assert bridge.get(volatile["id"]) is None


def test_in_memory_only_bridge_remains_functional_without_disk():
    bridge = bm.Bridge("pure-memory", data_dir=None)
    first = bridge.post("agent", "finding", "memory")
    second = bridge.post("agent", "answer", "reply", refs=[first["id"]])
    assert [p["id"] for p in bridge.posts()] == [first["id"], second["id"]]
