"""Additional path-resolution edge cases for the bridge's best-effort journal."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from avrone.bridge import Bridge, blackboard_posts


def _core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


def test_bridge_parent_is_a_file_but_in_memory_post_still_works(tmp_path):
    (tmp_path / "bridge").write_text("not a directory")
    bridge = Bridge("name", tmp_path)
    assert bridge.path is None
    written = bridge.post("agent", "finding", "kept")
    assert bridge.get(written["id"]) is not None
    assert blackboard_posts(_core(tmp_path)) == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_dangling_bridge_directory_symlink_falls_back_to_memory(tmp_path):
    (tmp_path / "bridge").symlink_to(tmp_path / "missing", target_is_directory=True)
    bridge = Bridge("name", tmp_path)
    assert bridge.path is None
    assert bridge.post("agent", "finding", "kept")["text"] == "kept"
    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_self_referential_journal_symlink_does_not_crash_fanin(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    path = folder / "loop.jsonl"
    path.symlink_to(path)

    bridge = Bridge("loop", tmp_path)
    assert bridge.posts() == []
    bridge.post("agent", "finding", "kept in memory")
    assert len(bridge.posts()) == 1
    assert blackboard_posts(_core(tmp_path)) == []
    assert path.is_symlink()
