"""Atomic cursor-save failure is not a successful global acknowledgment."""
from __future__ import annotations

from collections import OrderedDict
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=root))


def _post(pid, ts=1):
    return {"id": pid, "run_id": "cursor-durability", "agent": "writer",
            "kind": "finding", "text": pid, "refs": [], "ts": ts}


def _cursor_fault(monkeypatch):
    real = bm.os.replace
    blocked = [True]
    def failing_replace(src, dst):
        if blocked[0] and Path(dst).name == "_fanin_order.json":
            raise OSError(errno.EIO, "simulated failed atomic cursor rename")
        return real(src, dst)
    monkeypatch.setattr(bm.os, "replace", failing_replace)
    return blocked


def test_new_global_post_not_acknowledged_when_cursor_replace_fails(tmp_path, monkeypatch):
    core = _core(tmp_path)
    blocked = _cursor_fault(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("written-but-unacknowledged"))
    assert not (tmp_path / "bridge" / "_fanin_order.json").exists()
    assert bm.Bridge("cursor-durability", tmp_path).get("written-but-unacknowledged") is not None
    blocked[0] = False
    assert [p["id"] for p in bm.blackboard_posts(core)] == ["written-but-unacknowledged"]
    assert (tmp_path / "bridge" / "_fanin_order.json").exists()


def test_failed_save_preserves_prior_cursor_and_retries_on_same_core(tmp_path, monkeypatch):
    core = _core(tmp_path)
    assert bm.blackboard_accept(core, _post("first", ts=9999)) == (False, "first")
    blocked = _cursor_fault(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("later", ts=1))
    assert list(bm._load_order(core)) == ["first"]
    assert not list((tmp_path / "bridge").glob(".*.tmp"))
    blocked[0] = False
    assert [p["id"] for p in bm.blackboard_posts(core, since="first")] == ["later"]
    assert list(bm._load_order(_core(tmp_path))) == ["first", "later"]


def test_read_cannot_return_unpersisted_first_snapshot_cursor(tmp_path, monkeypatch):
    bm.Bridge("cursor-durability", tmp_path).post("worker", "finding", "already-durable")
    core = _core(tmp_path)
    blocked = _cursor_fault(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_posts(core)
    blocked[0] = False
    rows = bm.blackboard_posts(core)
    assert len(rows) == 1
    assert bm._load_order(_core(tmp_path))


def test_direct_cursor_save_reports_failure_and_invalidates_cached_revision(tmp_path, monkeypatch):
    core = _core(tmp_path)
    bm._load_order(core)
    blocked = _cursor_fault(monkeypatch)
    assert bm._save_order(core, OrderedDict([("event", None)])) is False
    assert core._blackboard_order_revision is None
    blocked[0] = False
    assert bm._save_order(core, OrderedDict([("event", None)])) is True
    assert list(bm._load_order(_core(tmp_path))) == ["event"]


def test_memory_only_global_cursor_needs_no_file(tmp_path):
    core = _core(None)
    assert bm.blackboard_accept(core, _post("pure-memory")) == (False, "pure-memory")
    assert [p["id"] for p in bm.blackboard_posts(core)] == ["pure-memory"]
    assert bm._save_order(core, OrderedDict([("pure-memory", None)])) is True
