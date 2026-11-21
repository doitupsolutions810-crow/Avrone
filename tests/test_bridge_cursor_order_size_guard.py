"""Cursor writer must never exceed the same byte bound as the cursor reader."""
from __future__ import annotations

from collections import OrderedDict
import json
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=root))


def _ids(*names):
    return OrderedDict((name, None) for name in names)


def _post(pid):
    return {"id": pid, "run_id": "cursor-size", "agent": "worker",
            "kind": "finding", "text": pid, "refs": [], "ts": 1}


def test_cursor_save_at_exact_byte_cap_is_readable(tmp_path, monkeypatch):
    order = _ids("alpha", "beta", "gamma")
    exact = len(json.dumps({"ids": list(order)}).encode("utf-8"))
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", exact)
    core = _core(tmp_path)
    assert bm._save_order(core, order) is True
    path = bm._order_path(core)
    assert path.stat().st_size == exact
    assert path.read_text() == json.dumps({"ids": list(order)})
    assert list(bm._load_order(_core(tmp_path))) == list(order)


def test_cursor_save_one_byte_over_limit_fails_without_creating_registry(tmp_path, monkeypatch):
    order = _ids("alpha", "beta", "gamma")
    exact = len(json.dumps({"ids": list(order)}).encode("utf-8"))
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", exact - 1)
    core = _core(tmp_path)
    assert bm._save_order(core, order) is False
    assert not bm._order_path(core).exists()
    assert not list((tmp_path / "bridge").glob(".*.tmp"))


def test_oversized_cursor_cannot_replace_existing_valid_order(tmp_path, monkeypatch):
    core = _core(tmp_path)
    earlier = _ids("old-id")
    exact = len(json.dumps({"ids": list(earlier)}).encode("utf-8"))
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", exact)
    assert bm._save_order(core, earlier) is True
    assert bm._save_order(core, _ids("old-id", "new-id")) is False
    assert core._blackboard_order_revision is None
    assert list(bm._load_order(_core(tmp_path))) == ["old-id"]


def test_global_admission_denies_cursor_overflow_and_recovers(tmp_path, monkeypatch):
    core = _core(tmp_path)
    single_size = len(json.dumps({"ids": ["alpha"]}).encode("utf-8"))
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", single_size)
    assert bm.blackboard_accept(core, _post("alpha")) == (False, "alpha")
    with pytest.raises(bm.BridgeError, match="global cursor not persisted"):
        bm.blackboard_accept(core, _post("beta"))
    assert list(bm._load_order(_core(tmp_path))) == ["alpha"]
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", 16 * 1024 * 1024)
    assert [p["id"] for p in bm.blackboard_posts(core, since="alpha")] == ["beta"]
    assert list(bm._load_order(_core(tmp_path))) == ["alpha", "beta"]


def test_streamed_cursor_format_matches_json_dumps_over_multiple_sizes(tmp_path):
    core = _core(tmp_path)
    for count in (0, 1, 2, 10, 80):
        names = [f"entry-{i}" for i in range(count)]
        assert bm._save_order(core, _ids(*names)) is True
        raw = bm._order_path(core).read_bytes()
        assert raw == json.dumps({"ids": names}).encode("utf-8")
        assert list(bm._load_order(_core(tmp_path))) == names
