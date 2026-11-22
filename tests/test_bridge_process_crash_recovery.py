"""Crash-recovery regressions for journal writes, compaction and file locks.

Each subprocess terminates abruptly via os._exit; the test runner stays alive.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from avrone.bridge import Bridge


def _abrupt(script: str, root: Path, expected: int) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    run = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert run.returncode == expected, (run.stdout, run.stderr, run.returncode)


def test_crash_with_held_flock_releases_lock_for_next_writer(tmp_path):
    _abrupt("""
import os, sys
from avrone.bridge import Bridge
bridge = Bridge('crash-lock', sys.argv[1])
with bridge._file_lock():
    os._exit(71)
""", tmp_path, 71)
    bridge = Bridge("crash-lock", tmp_path)
    written = bridge.post("parent", "finding", "lock reclaimed")
    assert Bridge("crash-lock", tmp_path).get(written["id"]) is not None


def test_crash_before_compaction_replace_preserves_last_appended_post(tmp_path):
    _abrupt("""
import os, sys
from avrone.bridge import Bridge
bridge = Bridge('crash-replace', sys.argv[1], limit=1)
bridge.post('child', 'finding', 'first')
bridge.post('child', 'finding', 'second')
def die_at_rename(*args, **kwargs):
    os._exit(72)
os.replace = die_at_rename
bridge.post('child', 'conclusion', 'last-survives')
""", tmp_path, 72)
    bridge = Bridge("crash-replace", tmp_path, limit=1)
    assert [p["text"] for p in bridge.posts()] == ["last-survives"]
    assert len(Bridge("crash-replace", tmp_path, limit=1).posts()) == 1


def test_crash_after_partial_append_allows_next_valid_post(tmp_path):
    _abrupt("""
import os, sys
from avrone.bridge import Bridge
bridge = Bridge('crash-append', sys.argv[1])
bridge.post('child', 'finding', 'first-survives')
with bridge.path.open('ab') as stream:
    stream.write(b'{"id":"partial","kind":"finding","text":"unfinished')
    stream.flush()
    os.fsync(stream.fileno())
os._exit(73)
""", tmp_path, 73)
    bridge = Bridge("crash-append", tmp_path)
    assert [p["text"] for p in bridge.posts()] == ["first-survives"]
    second = bridge.post("parent", "conclusion", "second-survives")
    restored = Bridge("crash-append", tmp_path)
    assert [p["text"] for p in restored.posts()] == ["first-survives", "second-survives"]
    assert restored.get(second["id"]) is not None
