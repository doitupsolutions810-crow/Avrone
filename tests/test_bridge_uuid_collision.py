"""Collision-injected Bridge post IDs must never overwrite or resurrect posts."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _uuid_sequence(monkeypatch, values):
    iterator = iter(values)
    monkeypatch.setattr(bm.uuid, "uuid4", lambda: SimpleNamespace(hex=next(iterator)))


def test_live_post_id_collision_retries_without_overwriting(tmp_path, monkeypatch):
    a, b = "a" * 12, "b" * 12
    _uuid_sequence(monkeypatch, [a, a, b])
    bridge = bm.Bridge("live-collision", tmp_path, limit=8)
    first = bridge.post("original", "finding", "trusted")
    second = bridge.post("second", "finding", "new")
    assert (first["id"], second["id"]) == (a, b)
    assert bridge.get(a)["text"] == "trusted"
    assert bridge.get(b)["text"] == "new"
    reopened = bm.Bridge("live-collision", tmp_path, limit=8)
    assert [p["text"] for p in reopened.posts()] == ["trusted", "new"]


def test_tombstoned_id_collision_retries_before_compaction(tmp_path, monkeypatch):
    a, b, c = "a" * 12, "b" * 12, "c" * 12
    _uuid_sequence(monkeypatch, [a, b, a, c])
    bridge = bm.Bridge("tomb-collision", tmp_path, limit=1)
    bridge.post("worker", "finding", "first")
    bridge.post("worker", "finding", "second")
    third = bridge.post("worker", "finding", "third")
    assert third["id"] == c
    assert bridge.get(a) is None
    assert bridge.get(c)["text"] == "third"
    reopened = bm.Bridge("tomb-collision", tmp_path, limit=1)
    assert [p["id"] for p in reopened.posts()] == [c]


def test_permanent_uuid_collision_fails_closed_and_preserves_journal(tmp_path, monkeypatch):
    token = "a" * 12
    monkeypatch.setattr(bm.uuid, "uuid4", lambda: SimpleNamespace(hex=token))
    bridge = bm.Bridge("exhausted-ids", tmp_path, limit=8)
    first = bridge.post("worker", "finding", "keep")
    with pytest.raises(bm.BridgeError, match="unique post id"):
        bridge.post("worker", "finding", "must-not-overwrite")
    assert [p["id"] for p in bridge.posts()] == [first["id"]]
    reopened = bm.Bridge("exhausted-ids", tmp_path, limit=8)
    assert [p["text"] for p in reopened.posts()] == ["keep"]


@pytest.mark.parametrize("bad", ["a b c", "invalid!!id", "", "../../escape"])
def test_invalid_generated_identifier_cannot_be_persisted(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(bm.uuid, "uuid4", lambda: SimpleNamespace(hex=bad))
    bridge = bm.Bridge("invalid-uuid", tmp_path)
    with pytest.raises(bm.BridgeError, match="unique post id"):
        bridge.post("worker", "finding", "unsafe")
    assert bridge.posts() == []
    assert bm.Bridge("invalid-uuid", tmp_path).posts() == []
