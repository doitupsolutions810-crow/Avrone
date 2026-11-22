"""Global blackboard must never acknowledge an event it failed to persist."""
from __future__ import annotations

import errno
import os
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=path))


def _post(post_id):
    return {"id": post_id, "run_id": "durability-check", "agent": "worker",
            "kind": "finding", "text": post_id, "refs": [], "ts": 1}


def _fail_journal_append(monkeypatch, run_id="durability-check"):
    real = bm._regular_fd
    blocked = [True]
    def maybe_fail(path, flags):
        if (blocked[0] and str(path).endswith(".jsonl")
                and flags & os.O_APPEND):
            raise OSError(errno.ENOSPC, "simulated disk full")
        return real(path, flags)
    monkeypatch.setattr(bm, "_regular_fd", maybe_fail)
    return blocked


def test_global_admission_fails_on_journal_write_error_and_is_not_visible(tmp_path, monkeypatch):
    core = _core(tmp_path)
    blocked = _fail_journal_append(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global post not persisted"):
        bm.blackboard_accept(core, _post("failed-event"))
    assert bm.blackboard_posts(core) == []
    assert not (tmp_path / "bridge" / "_fanin_order.json").exists()
    blocked[0] = False
    assert bm.blackboard_accept(core, _post("failed-event")) == (False, "failed-event")
    assert [p["id"] for p in bm.blackboard_posts(_core(tmp_path))] == ["failed-event"]


def test_existing_durable_posts_survive_rejected_followup(tmp_path, monkeypatch):
    core = _core(tmp_path)
    assert bm.blackboard_accept(core, _post("durable-before"))[0] is False
    blocked = _fail_journal_append(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global post not persisted"):
        bm.blackboard_accept(core, _post("rejected-after"))
    assert [p["id"] for p in bm.blackboard_posts(core)] == ["durable-before"]
    assert [p["id"] for p in bm.blackboard_posts(_core(tmp_path))] == ["durable-before"]
    blocked[0] = False


def test_failed_sidecar_transaction_does_not_ack_global_write(tmp_path, monkeypatch):
    original = bm.Bridge._file_lock
    from contextlib import contextmanager
    @contextmanager
    def failed_lock(self):
        raise OSError(errno.EACCES, "run sidecar denied")
        yield
    monkeypatch.setattr(bm.Bridge, "_file_lock", failed_lock)
    with pytest.raises(bm.BridgeError, match="global post not persisted"):
        bm.blackboard_accept(_core(tmp_path), _post("unlocked"))
    monkeypatch.setattr(bm.Bridge, "_file_lock", original)
    assert bm.blackboard_posts(_core(tmp_path)) == []


def test_memory_only_global_admission_still_acknowledges(tmp_path):
    core = _core(None)
    assert bm.blackboard_accept(core, _post("memory-id")) == (False, "memory-id")
    assert [p["id"] for p in bm.blackboard_posts(core)] == ["memory-id"]


def test_disk_admission_ack_matches_restart_state(tmp_path):
    core = _core(tmp_path)
    for i in range(12):
        assert bm.blackboard_accept(core, _post(f"record-{i}"))[0] is False
    persisted = bm.blackboard_posts(_core(tmp_path))
    assert [p["id"] for p in persisted] == [f"record-{i}" for i in range(12)]
