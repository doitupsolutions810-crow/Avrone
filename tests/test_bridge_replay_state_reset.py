"""Replaying a journal must reset obsolete tombstone state.

An externally restored/replaced journal is the new durable source of truth.
Past in-memory evictions must not suppress valid IDs that the replacement now
contains, while tombstones physically present in that journal stay final.
"""
from __future__ import annotations

import json
import os
import random

import pytest

from avrone import bridge as bm


def _record(pid, n=1, run_id="reset-replay"):
    return {"id": pid, "run_id": run_id, "agent": "writer",
            "kind": "finding", "text": f"post-{pid}", "refs": [], "ts": n}


def _lines(*records):
    return "".join(json.dumps(p) + "\n" for p in records)


def test_replay_drops_evictions_from_previous_journal():
    b = bm.Bridge("reset-replay", limit=1)
    b._replay(_lines(_record("old-a"), _record("old-b", 2)))
    assert b.get("old-a") is None
    assert "old-a" in b._evicted
    b._replay(_lines(_record("old-a", 3)))
    assert [p["id"] for p in b.posts()] == ["old-a"]
    assert "old-a" not in b._evicted


def test_replacement_journal_reopens_id_evicted_by_older_file(tmp_path):
    b = bm.Bridge("reset-replay", tmp_path, limit=1)
    a = b.post("worker", "finding", "original-a")
    b.post("worker", "finding", "original-b")
    assert b.get(a["id"]) is None
    replacement = b.path.with_suffix(".replacement")
    replacement.write_text(_lines(_record(a["id"], run_id="reset-replay")))
    os.replace(replacement, b.path)
    assert b.get(a["id"])["text"] == f"post-{a['id']}"
    assert [p["id"] for p in bm.Bridge("reset-replay", tmp_path, limit=1).posts()] == [a["id"]]


def test_replayed_tombstones_still_block_resurrection():
    b = bm.Bridge("reset-replay", limit=5)
    b._replay(_lines(_record("previous")))
    blocked = json.dumps({"tombstones": ["blocked"]}) + "\n"
    b._replay(blocked + _lines(_record("blocked"), _record("survivor", 2)))
    assert b.get("blocked") is None
    assert [p["id"] for p in b.posts()] == ["survivor"]
    assert "blocked" in b._evicted


def test_replayed_compaction_retains_tombstones_for_evicted_records(tmp_path):
    b = bm.Bridge("reset-replay", tmp_path, limit=1)
    a = b.post("worker", "finding", "a")
    b.post("worker", "finding", "b")
    c = b.post("worker", "finding", "c")
    assert b.get(a["id"]) is None
    restored = bm.Bridge("reset-replay", tmp_path, limit=1)
    assert [p["id"] for p in restored.posts()] == [c["id"]]
    assert restored.merge_remote([dict(a)], "peer")["skipped"] == 1


@pytest.mark.parametrize("seed", [0, 1, 7, 19, 31])
def test_repeated_replay_equals_fresh_instance_despite_past_tombstones(seed):
    rng = random.Random(seed)
    stale = bm.Bridge("reset-replay", limit=4)
    stale._replay(_lines(*[_record(f"historical-{i}", i+1) for i in range(20)]))
    assert stale._evicted

    fresh = bm.Bridge("reset-replay", limit=4)
    records = [_record(f"candidate-{i}", i+1) for i in range(30)]
    rng.shuffle(records)
    journal = _lines(*records)
    stale._replay(journal)
    fresh._replay(journal)
    assert stale.posts() == fresh.posts()
    assert list(stale._evicted) == list(fresh._evicted)


def test_repeated_replay_does_not_retain_old_manual_tombstone():
    b = bm.Bridge("reset-replay", limit=10)
    b._replay(json.dumps({"tombstones": ["expired"]}) + "\n")
    assert "expired" in b._evicted
    b._replay(_lines(_record("expired")))
    assert b.get("expired") is not None
    assert "expired" not in b._evicted
