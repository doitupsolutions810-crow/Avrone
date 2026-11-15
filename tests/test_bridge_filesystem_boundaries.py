"""Filesystem adversarial regressions for AVRONE's per-run bridge journal.

The CodeX event log already denies unsafe paths; the learning bridge must
maintain the same boundary without losing its in-memory behavior.
"""
from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from avrone.bridge import Bridge, blackboard_posts


def _foreign_record() -> str:
    return json.dumps({"id": "foreign", "run_id": "other", "agent": "x",
                       "kind": "finding", "text": "outside-private-post",
                       "ts": 1, "refs": []}) + "\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_symlinked_journal_is_neither_replayed_nor_written(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_foreign_record())
    before = outside.read_bytes()
    (folder / "journal.jsonl").symlink_to(outside)

    bridge = Bridge("journal", tmp_path)
    assert bridge.posts() == []
    bridge.post("worker", "finding", "in-memory still works")
    assert len(bridge.posts()) == 1
    assert outside.read_bytes() == before

    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    assert blackboard_posts(core) == []
    assert outside.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_symlinked_lock_sidecar_cannot_redirect_journal_write(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    outside = tmp_path / "foreign-lock"
    outside.write_text("do not touch")
    (folder / "journal.jsonl.lock").symlink_to(outside)

    bridge = Bridge("journal", tmp_path)
    bridge.post("worker", "finding", "retained in memory")
    assert len(bridge.posts()) == 1
    assert outside.read_text() == "do not touch"
    assert not (folder / "journal.jsonl").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_symlinked_bridge_directory_is_not_a_persistence_root(tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    (tmp_path / "bridge").symlink_to(outside, target_is_directory=True)

    bridge = Bridge("journal", tmp_path)
    bridge.post("worker", "finding", "memory-only")
    assert len(bridge.posts()) == 1
    assert bridge.path is None
    assert list(outside.iterdir()) == []

    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    assert blackboard_posts(core) == []
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink support required")
def test_hardlinked_journal_cannot_read_or_change_foreign_inode(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    outside = tmp_path / "foreign.jsonl"
    outside.write_text(_foreign_record())
    target = folder / "journal.jsonl"
    try:
        os.link(outside, target)
    except OSError as exc:
        pytest.skip(f"hardlink not supported on this filesystem: {exc}")
    before = outside.read_bytes()

    bridge = Bridge("journal", tmp_path)
    assert bridge.posts() == []
    bridge.post("worker", "finding", "memory-only")
    assert outside.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_fifo_journal_is_rejected_without_blocking(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    fifo = folder / "journal.jsonl"
    os.mkfifo(fifo)
    bridge = Bridge("journal", tmp_path)
    assert bridge.posts() == []
    bridge.post("worker", "finding", "memory-only")
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    assert len(bridge.posts()) == 1


def test_regular_journal_still_compacts_and_survives_reopen(tmp_path):
    bridge = Bridge("ordinary", tmp_path, limit=3)
    entries = [bridge.post("worker", "finding", f"post {n}") for n in range(13)]
    reopened = Bridge("ordinary", tmp_path, limit=3)
    assert [post["id"] for post in reopened.posts()] == [post["id"] for post in entries[-3:]]
    assert len(reopened.posts()) == 3
    assert (tmp_path / "bridge" / "ordinary.jsonl").is_file()
