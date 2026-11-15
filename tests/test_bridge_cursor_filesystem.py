"""The fan-in cursor registry must honor the same inode protections as journals."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from avrone.bridge import Bridge, _load_order, blackboard_posts


def core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


@pytest.mark.parametrize("candidate", ["symlink", "hardlink", "fifo"])
def test_unsafe_cursor_registry_is_not_read(tmp_path, candidate):
    if candidate in ("symlink", "hardlink") and not hasattr(os, "symlink"):
        pytest.skip("link support required")
    if candidate == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO support required")
    folder = tmp_path / "bridge"
    folder.mkdir()
    target = folder / "_fanin_order.json"
    outside = tmp_path / "foreign-order.json"
    outside.write_text(json.dumps({"ids": ["injected"]}), encoding="utf-8")
    before = outside.read_bytes()
    if candidate == "symlink":
        target.symlink_to(outside)
    elif candidate == "hardlink":
        try:
            os.link(outside, target)
        except OSError as exc:
            pytest.skip(f"hardlinks unavailable: {exc}")
    else:
        os.mkfifo(target)

    holder = core(tmp_path)
    assert list(_load_order(holder)) == []
    assert outside.read_bytes() == before


def test_safe_registry_still_replays_cursor_order(tmp_path):
    bridge = Bridge("persisted", tmp_path)
    first = bridge.post("agent", "finding", "first")
    second = bridge.post("agent", "finding", "second")
    holder = core(tmp_path)
    assert [p["id"] for p in blackboard_posts(holder)] == [first["id"], second["id"]]

    restarted = core(tmp_path)
    assert [p["id"] for p in blackboard_posts(restarted)] == [first["id"], second["id"]]
    assert (tmp_path / "bridge" / "_fanin_order.json").is_file()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_registry_symlink_is_replaced_not_followed_on_sync(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    outside = tmp_path / "foreign.json"
    outside.write_text("never modify")
    (folder / "_fanin_order.json").symlink_to(outside)

    bridge = Bridge("one", tmp_path)
    post = bridge.post("agent", "finding", "one")
    holder = core(tmp_path)
    assert [p["id"] for p in blackboard_posts(holder)] == [post["id"]]
    assert outside.read_text() == "never modify"
    saved = json.loads((folder / "_fanin_order.json").read_text())
    assert saved == {"ids": [post["id"]]}
