"""Streaming journal replay must remain bounded under corrupted or huge records."""
from __future__ import annotations

import json
import os

import pytest

from avrone import bridge as bm


class _NoSlurp:
    """Forbid reading an entire journal while preserving normal fd ownership."""

    def __init__(self, underlying):
        self._file = underlying

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._file.__exit__(*args)

    def read(self, *args, **kwargs):
        raise AssertionError("journal replay called unbounded read()")

    def readline(self, *args, **kwargs):
        return self._file.readline(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._file, name)


@pytest.fixture
def guard_journal_reads(monkeypatch):
    original = bm.os.fdopen
    def guarded(fd, mode, *args, **kwargs):
        stream = original(fd, mode, *args, **kwargs)
        return _NoSlurp(stream) if mode == "r" else stream
    monkeypatch.setattr(bm.os, "fdopen", guarded)


def test_reopen_does_not_slurp_journal(tmp_path, guard_journal_reads):
    journal = bm.Bridge("stream-reopen", tmp_path)
    first = journal.post("writer", "finding", "known-content")
    reopened = bm.Bridge("stream-reopen", tmp_path)
    assert [p["id"] for p in reopened.posts()] == [first["id"]]


def test_compaction_does_not_slurp_journal(tmp_path, guard_journal_reads):
    journal = bm.Bridge("stream-compact", tmp_path, limit=1)
    for i in range(4):
        journal.post("writer", "finding", f"data-{i}")
    assert [p["text"] for p in bm.Bridge("stream-compact", tmp_path, limit=1).posts()] == ["data-3"]


def test_oversized_line_is_skipped_and_later_post_survives(tmp_path):
    journal = bm.Bridge("huge-line", tmp_path)
    item = journal.post("writer", "finding", "before corruption")
    second = {"id":"safe-2","run_id":"huge-line","agent":"writer",
              "kind":"finding","text":"after corruption","ts":1,"refs":[]}
    with journal.path.open("ab") as stream:
        stream.write(b'{"id":"huge","text":"' + b"x" * (bm.MAX_JOURNAL_LINE_CHARS + 25) + b'"}\n')
        stream.write((json.dumps(second) + "\n").encode("utf-8"))
    reopened = bm.Bridge("huge-line", tmp_path)
    assert [p["id"] for p in reopened.posts()] == [item["id"], "safe-2"]


def test_valid_eof_without_final_newline_is_replayed(tmp_path):
    journal = bm.Bridge("valid-eof", tmp_path)
    post = journal.post("writer", "finding", "no-newline")
    path = journal.path
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    path.write_bytes(raw[:-1])
    assert bm.Bridge("valid-eof", tmp_path).get(post["id"])["text"] == "no-newline"


@pytest.mark.parametrize("size", [1, 129, 10_000, 1_000_000])
def test_giant_corrupt_tail_is_not_replayed(tmp_path, size):
    bridge = bm.Bridge("corrupt-tail", tmp_path)
    item = bridge.post("agent", "finding", "valid")
    with bridge.path.open("ab") as f:
        f.write(b"a" * size)
    assert [p["id"] for p in bm.Bridge("corrupt-tail", tmp_path).posts()] == [item["id"]]




@pytest.mark.parametrize("length", [
    bm.MAX_JOURNAL_LINE_CHARS - 1,
    bm.MAX_JOURNAL_LINE_CHARS,
    bm.MAX_JOURNAL_LINE_CHARS + 1,
    bm.MAX_JOURNAL_LINE_CHARS * 3,
])
def test_bounded_line_iterator_edge_sizes(length):
    import io
    data = "x" * length + "\n" + "tail\n"
    lines = list(bm._journal_lines(io.StringIO(data)))
    expected = ["x" * length + "\n", "tail\n"] if length < bm.MAX_JOURNAL_LINE_CHARS else ["tail\n"]
    assert lines == expected


def test_very_long_local_node_id_is_clamped_before_journal_write(tmp_path):
    node = "LONG_NODE_" * 30_000
    bridge = bm.Bridge("node-cap", tmp_path, node_id=node)
    posted = bridge.post("writer", "finding", "persisted")
    reopened = bm.Bridge("node-cap", tmp_path)
    assert len(posted["origin_node"]) <= 80
    assert reopened.get(posted["id"])["text"] == "persisted"


def test_compaction_recovers_after_giant_junk_line(tmp_path):
    bridge = bm.Bridge("long-junk", tmp_path, limit=1)
    bridge.post("writer", "finding", "first")
    with bridge.path.open("ab") as stream:
        stream.write(b"b" * (bm.MAX_JOURNAL_LINE_CHARS * 4) + b"\n")
    for n in range(4):
        bridge.post("writer", "finding", f"record-{n}")
    assert [p["text"] for p in bm.Bridge("long-junk", tmp_path, limit=1).posts()] == ["record-3"]




def test_streamed_replay_matches_legacy_semantics_across_64_corrupt_journals():
    import io
    import random
    from collections import OrderedDict

    for seed in range(64):
        rng = random.Random(seed)
        lines = []
        ids = []
        for n in range(40):
            pid = f"post-{seed}-{n}"
            item = {"id": pid, "run_id": "metamorphic", "agent": "writer",
                    "kind": "finding", "text": f"v-{n}", "ts": n + 1,
                    "refs": []}
            ids.append(pid)
            lines.append(json.dumps(item) + "\n")
            if rng.random() < 0.18:
                lines.append(json.dumps(dict(item, text="duplicate-must-not-overwrite")) + "\n")
            if rng.random() < 0.12:
                lines.append(json.dumps({"tombstones": [rng.choice(ids)]}) + "\n")
            if rng.random() < 0.25:
                lines.append(rng.choice(["not-json", "{broken", "[]", "null", ""]) + "\n")
        if seed % 8 == 0:
            lines.insert(3, "x" * (bm.MAX_JOURNAL_LINE_CHARS + 3) + "\n")
        source = "".join(lines)

        baseline = bm.Bridge("metamorphic", limit=27)
        baseline._replay(source)
        streamed = bm.Bridge("metamorphic", limit=27)
        streamed._replay(bm._journal_lines(io.StringIO(source)))
        assert streamed.posts() == baseline.posts(), f"seed={seed}"
        assert list(streamed._evicted) == list(baseline._evicted), f"seed={seed}"




def test_extreme_run_identifier_remains_durable_after_restart(tmp_path):
    run_id = "run-" + "U" * (bm.MAX_JOURNAL_LINE_CHARS + 200)
    bridge = bm.Bridge(run_id, tmp_path)
    saved = bridge.post("writer", "finding", "long-run-is-durable")
    assert len(bridge.run_id) <= bm.MAX_RUN_ID_CHARS
    assert bm.Bridge(run_id, tmp_path).get(saved["id"])["text"] == "long-run-is-durable"


def test_long_run_identifiers_with_common_prefix_remain_isolated(tmp_path):
    shared = "same-prefix-" + "x" * (bm.MAX_JOURNAL_LINE_CHARS + 100)
    a = bm.Bridge(shared + "-a", tmp_path)
    b = bm.Bridge(shared + "-b", tmp_path)
    a_post = a.post("writer", "finding", "only-a")
    b_post = b.post("writer", "finding", "only-b")
    assert a.path != b.path
    assert {p["text"] for p in bm.Bridge(shared + "-a", tmp_path).posts()} == {"only-a"}
    assert {p["text"] for p in bm.Bridge(shared + "-b", tmp_path).posts()} == {"only-b"}
    assert a_post["id"] != b_post["id"]

