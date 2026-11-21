"""A Core's global cursor cache must notice changes made by another Core.

This also protects cursor monotonicity when later accepted remote posts carry
older timestamps: their admission order must remain after previously sent IDs.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import os
from types import SimpleNamespace

from avrone import bridge as bm


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=root))


def test_two_core_objects_observe_newly_saved_registry(tmp_path):
    first, second = _core(tmp_path), _core(tmp_path)
    assert list(bm._load_order(second)) == []
    bm._save_order(first, OrderedDict([("external-id", None)]))
    assert list(bm._load_order(second)) == ["external-id"]


def test_cached_core_sees_atomic_replacement_even_when_byte_length_is_equal(tmp_path):
    core = _core(tmp_path)
    path = bm._order_path(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": ["aaaa"]}), encoding="utf-8")
    assert list(bm._load_order(core)) == ["aaaa"]
    alternate = path.with_suffix(".replacement")
    alternate.write_text(json.dumps({"ids": ["bbbb"]}), encoding="utf-8")
    assert path.stat().st_size == alternate.stat().st_size
    os.replace(alternate, path)
    assert list(bm._load_order(core)) == ["bbbb"]


def test_deleted_cursor_file_invalidates_cached_order(tmp_path):
    core = _core(tmp_path)
    path = bm._order_path(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": ["outdated"]}), encoding="utf-8")
    assert list(bm._load_order(core)) == ["outdated"]
    path.unlink()
    assert list(bm._load_order(core)) == []


def test_malformed_external_update_does_not_retain_stale_order(tmp_path):
    core = _core(tmp_path)
    path = bm._order_path(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": ["previous"]}), encoding="utf-8")
    assert list(bm._load_order(core)) == ["previous"]
    alternate = path.with_suffix(".new")
    alternate.write_bytes(b"\xff\xfeinvalid-json")
    os.replace(alternate, path)
    assert list(bm._load_order(core)) == []


def test_in_memory_cursor_cache_remains_reusable():
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=None))
    initial = bm._load_order(core)
    initial["memory-only"] = None
    assert bm._load_order(core) is initial


def test_second_core_preserves_cursor_when_new_remote_ts_is_older(tmp_path):
    first, stale = _core(tmp_path), _core(tmp_path)
    assert bm.blackboard_posts(stale) == []
    event_a = {"id": "cursor-a", "run_id": "run-a", "kind": "finding",
               "text": "first-arrival", "agent": "agent-a", "ts": 9999}
    duplicate_a, _ = bm.blackboard_accept(first, event_a)
    assert not duplicate_a
    assert [p["id"] for p in bm.blackboard_posts(first)] == ["cursor-a"]

    event_b = {"id": "cursor-b", "run_id": "run-b", "kind": "finding",
               "text": "late-arrival-with-old-ts", "agent": "agent-b", "ts": 1}
    duplicate_b, _ = bm.blackboard_accept(stale, event_b)
    assert not duplicate_b
    assert [p["id"] for p in bm.blackboard_posts(first, since="cursor-a")] == ["cursor-b"]
    assert [p["id"] for p in bm.blackboard_posts(stale)] == ["cursor-a", "cursor-b"]
