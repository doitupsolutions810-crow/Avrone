"""Codex audit log adversarial persistence and input budget contracts.

Tests are intentionally red on unsafe legacy behavior: hardlinked logs must not
read or modify external files, malformed limits must not crash, and writes after
a torn JSONL tail must remain independently replayable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from avrone.codex import Codex, MAX_LINE_BYTES


@pytest.mark.parametrize("bad", [
    float("inf"), float("-inf"), float("nan"), "9" * 5000,
    [], {}, object(),
], ids=["inf", "minus-inf", "nan", "huge-int", "list", "dict", "object"])
def test_replay_bad_limit_falls_back_without_throwing(tmp_path, bad):
    c = Codex(tmp_path)
    c.emit("stable", "one", "a")
    assert len(c.replay("stable", limit=bad)) == 1


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), "9" * 5000, object()])
def test_runs_bad_limit_falls_back_without_throwing(tmp_path, bad):
    c = Codex(tmp_path)
    c.emit("stable", "one", "a")
    assert [r["run_id"] for r in c.runs(limit=bad)] == ["stable"]


def test_hardlinked_audit_log_never_modifies_another_file(tmp_path):
    c = Codex(tmp_path)
    outside = tmp_path / "foreign.jsonl"
    outside.write_text("FOREIGN AUDIT CONTENT\n", encoding="utf-8")
    link = c.log_dir / "foreign-run.jsonl"
    try:
        os.link(outside, link)
    except OSError as exc:
        pytest.skip(f"hardlinks unsupported: {exc}")
    before = outside.read_bytes()
    with pytest.raises((OSError, ValueError)):
        c.emit("foreign-run", "run.start", "agent", data="unsafe")
    assert outside.read_bytes() == before
    assert c.replay("foreign-run") == []
    assert not any(r["run_id"] == "foreign-run" for r in c.runs())


def test_torn_tail_is_recovered_before_future_valid_event(tmp_path):
    c = Codex(tmp_path)
    c.emit("torn", "run.start", "first")
    with (c.log_dir / "torn.jsonl").open("ab") as stream:
        stream.write(b'{"run_id":"torn","kind":"partial')
        stream.flush()
    c.emit("torn", "run.done", "second")
    assert [e["kind"] for e in Codex(tmp_path).replay("torn")] == ["run.start", "run.done"]


def test_oversized_payload_does_not_become_unreplayable_audit_event(tmp_path):
    c = Codex(tmp_path)
    c.emit("big", "tool.result", "agent", output="Z" * (MAX_LINE_BYTES + 100))
    events = Codex(tmp_path).replay("big")
    assert len(events) == 1
    assert events[0]["kind"] == "tool.result"
    assert (c.log_dir / "big.jsonl").stat().st_size <= MAX_LINE_BYTES


def test_fifo_path_refusal_never_blocks_audit_producer(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO only")
    c = Codex(tmp_path)
    os.mkfifo(c.log_dir / "pipe.jsonl")
    script = """
from avrone.codex import Codex
import pathlib, sys
try:
    Codex(pathlib.Path(sys.argv[1])).emit('pipe', 'run.start', 'agent')
except (OSError, ValueError):
    sys.exit(0)
sys.exit(1)
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                                capture_output=True, text=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("Codex writer hung while opening a hostile FIFO")
    assert result.returncode == 0, result.stderr




def _multiprocess_audit_writer(args):
    from avrone.codex import Codex
    root, worker, count = args
    c = Codex(Path(root))
    for i in range(count):
        c.emit("contended", "worker.event", worker, counter=i)
    return count


def test_six_audit_writers_do_not_interleave_jsonl_records(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=6, mp_context=ctx) as pool:
        amounts = list(pool.map(
            _multiprocess_audit_writer,
            [(str(tmp_path), f"worker-{i}", 85) for i in range(6)],
        ))
    assert sum(amounts) == 510
    c = Codex(tmp_path)
    events = c.replay("contended", limit=600)
    assert len(events) == 510
    assert len({e["id"] for e in events}) == 510
    assert {e["agent"] for e in events} == {f"worker-{i}" for i in range(6)}
    assert all(e["kind"] == "worker.event" for e in events)
    data = (c.log_dir / "contended.jsonl").read_text(encoding="utf-8")
    assert len(data.splitlines()) == 510
    for line in data.splitlines():
        assert isinstance(json.loads(line), dict)


def test_link_swapped_after_preflight_is_not_followed(tmp_path, monkeypatch):
    c = Codex(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("keep-safe", encoding="utf-8")
    original = c._log_path
    def swapped(run_id):
        path = original(run_id)
        path.symlink_to(outside)
        return path
    monkeypatch.setattr(c, "_log_path", swapped)
    with pytest.raises((OSError, ValueError)):
        c.emit("raced", "run.start", "agent")
    assert outside.read_text(encoding="utf-8") == "keep-safe"


def test_parent_log_folder_symlink_never_allows_outside_append(tmp_path):
    c = Codex(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    c.log_dir.rmdir()
    c.log_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        c.emit("escape", "run.start", "agent")
    assert list(other.iterdir()) == []


def test_aggregate_oversized_event_remains_replayable(tmp_path):
    c = Codex(tmp_path)
    huge = {f"key_{i}": "A" * 70_000 for i in range(105)}
    ev = c.emit("aggregate", "tool.result", "agent", output=huge)
    assert ev.data.get("audit_truncated") is True
    assert ev.data["original_bytes"] > MAX_LINE_BYTES
    saved = c.replay("aggregate")
    assert len(saved) == 1
    assert saved[0]["data"]["audit_truncated"] is True
    assert (c.log_dir / "aggregate.jsonl").stat().st_size <= MAX_LINE_BYTES

