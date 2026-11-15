"""Duplicate global admission must repair a failed cursor save before ACK."""
from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


def _post(pid, ts=1):
    return {"id": pid, "run_id": "cursor-retry", "agent": "writer",
            "kind": "finding", "text": pid, "ts": ts, "refs": []}


def _fail_cursor_replace(monkeypatch):
    original = bm.os.replace
    failures = [True]
    def faulty(src, dst):
        if failures[0] and Path(dst).name == "_fanin_order.json":
            raise OSError(errno.EIO, "simulated failed cursor replacement")
        return original(src, dst)
    monkeypatch.setattr(bm.os, "replace", faulty)
    return failures


def test_duplicate_retry_repairs_cursor_after_initial_save_failure(tmp_path, monkeypatch):
    core = _core(tmp_path)
    assert bm.blackboard_accept(core, _post("first", ts=9000)) == (False, "first")
    failures = _fail_cursor_replace(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("second", ts=1))
    assert [p["id"] for p in bm.Bridge("cursor-retry", tmp_path).posts()] == [
        "first", "second"
    ]
    failures[0] = False
    assert bm.blackboard_accept(core, _post("second", ts=1)) == (True, "second")
    assert [p["id"] for p in bm.blackboard_posts(core, since="first")] == ["second"]
    assert list(bm._load_order(_core(tmp_path))) == ["first", "second"]


def test_duplicate_retry_fails_closed_if_cursor_remains_unwritable(tmp_path, monkeypatch):
    core = _core(tmp_path)
    failures = _fail_cursor_replace(monkeypatch)
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("persisted-no-cursor"))
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("persisted-no-cursor"))
    failures[0] = False
    assert bm.blackboard_accept(core, _post("persisted-no-cursor")) == (
        True, "persisted-no-cursor"
    )
    assert list(bm._load_order(_core(tmp_path))) == ["persisted-no-cursor"]


def test_known_duplicate_with_durable_cursor_does_not_rewrite(tmp_path, monkeypatch):
    core = _core(tmp_path)
    assert bm.blackboard_accept(core, _post("already-stored")) == (False, "already-stored")
    def forbid(_core, _order):
        raise AssertionError("no rewrite should be required for an unchanged cursor")
    monkeypatch.setattr(bm, "_save_order", forbid)
    assert bm.blackboard_accept(core, _post("already-stored")) == (
        True, "already-stored"
    )
