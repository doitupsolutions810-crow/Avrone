"""Fail closed when global cross-process locking is unavailable.

A readable journal must not become an unsafe write target simply because its
sidecar lock is hostile, corrupt, or not lockable. Read fallback stays read-only.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=root))


def _post(post_id="lock-test"):
    return {"id": post_id, "run_id": "lock-run", "kind": "finding",
            "text": "must-not-write-unlocked", "agent": "worker", "ts": 1}


def _lock_path(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir(exist_ok=True)
    return folder / "_fanin_order.json.lock"


def test_symlink_sidecar_denies_disk_backed_global_admission(tmp_path):
    sidecar = _lock_path(tmp_path)
    protected = tmp_path / "protected"
    protected.write_text("unchanged")
    sidecar.symlink_to(protected)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())
    assert protected.read_text() == "unchanged"
    assert not list((tmp_path / "bridge").glob("*.jsonl"))


def test_hardlink_sidecar_denies_disk_backed_global_admission(tmp_path):
    sidecar = _lock_path(tmp_path)
    protected = tmp_path / "protected"
    protected.write_text("untouched")
    os.link(protected, sidecar)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())
    assert protected.read_text() == "untouched"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_fifo_sidecar_denies_without_blocking_or_writing(tmp_path):
    sidecar = _lock_path(tmp_path)
    os.mkfifo(sidecar)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())
    assert not list((tmp_path / "bridge").glob("*.jsonl"))


def test_lock_open_error_denies_admission_before_run_creation(tmp_path, monkeypatch):
    actual = bm._regular_fd
    def fail_sidecar(path, flags):
        if Path(path).name == "_fanin_order.json.lock":
            raise OSError(errno.EACCES, "access denied")
        return actual(path, flags)
    monkeypatch.setattr(bm, "_regular_fd", fail_sidecar)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())
    assert not list((tmp_path / "bridge").glob("*.jsonl"))


def test_lock_acquisition_error_denies_admission(tmp_path, monkeypatch):
    import fcntl
    real_flock = fcntl.flock
    def fail_global(fd, operation):
        if operation == fcntl.LOCK_EX:
            raise OSError(errno.EIO, "lock service unavailable")
        return real_flock(fd, operation)
    monkeypatch.setattr(fcntl, "flock", fail_global)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())


def test_reader_fallback_does_not_write_new_cursor(tmp_path):
    original = bm.Bridge("lock-run", tmp_path)
    original.post("writer", "finding", "already-on-disk")
    sidecar = _lock_path(tmp_path)
    protected = tmp_path / "do-not-touch"
    protected.write_text("safe")
    sidecar.symlink_to(protected)
    core = _core(tmp_path)
    assert [p["text"] for p in bm.blackboard_posts(core)] == ["already-on-disk"]
    assert not (tmp_path / "bridge" / "_fanin_order.json").exists()
    assert protected.read_text() == "safe"
    # Cache must remain clean so a later lock recovery persists the cursor.
    sidecar.unlink()
    assert [p["text"] for p in bm.blackboard_posts(core)] == ["already-on-disk"]
    assert (tmp_path / "bridge" / "_fanin_order.json").exists()


def test_normal_global_admission_still_persists_cursor_and_post(tmp_path):
    core = _core(tmp_path)
    duplicate, pid = bm.blackboard_accept(core, _post())
    assert not duplicate and pid == "lock-test"
    assert [p["id"] for p in bm.blackboard_posts(_core(tmp_path))] == [pid]
    assert (tmp_path / "bridge" / "_fanin_order.json").exists()


def test_memory_only_blackboard_needs_no_global_file_lock():
    core = _core(None)
    duplicate, pid = bm.blackboard_accept(core, _post())
    assert not duplicate and pid == "lock-test"
    assert [p["id"] for p in bm.blackboard_posts(core)] == [pid]


def test_unsafe_symlink_bridge_directory_denies_disk_admission(tmp_path):
    (tmp_path / "bridge").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(bm.BridgeError, match="global blackboard lock unavailable"):
        bm.blackboard_accept(_core(tmp_path), _post())
