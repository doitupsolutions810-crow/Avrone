"""Bounded prompt serialization and concurrent process compaction invariants.

No network is used. Independent processes compete on the same journal/lock files.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path

import pytest

from avrone.bridge import Bridge, compact


def _write_shared(args):
    root, worker, total = args
    bridge = Bridge("process-compact-stress", Path(root), limit=37)
    ids = []
    for n in range(total):
        ids.append(bridge.post(worker, "finding", f"{worker}#{n}")["id"])
    return ids


@pytest.mark.parametrize("budget", [-100, -1, 0, 1, 2, 3, 5, 9, 20])
def test_digest_never_exceeds_character_budget(tmp_path, budget):
    bridge = Bridge("digest-bound", tmp_path)
    bridge.post("agent", "conclusion", "SENSITIVE_CONTENT_" * 200)
    value = bridge.digest(max_chars=budget)
    assert isinstance(value, str)
    assert len(value) <= max(0, budget)


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), float("-inf"), {}, object()])
def test_digest_rejects_invalid_budget_without_crashing(tmp_path, budget):
    bridge = Bridge("digest-invalid", tmp_path)
    bridge.post("agent", "conclusion", "text")
    assert isinstance(bridge.digest(max_chars=budget), str)


@pytest.mark.parametrize("width", [-100, -1, 0])
def test_compact_negative_width_cannot_reveal_full_text(tmp_path, width):
    bridge = Bridge("compact-bound", tmp_path)
    post = bridge.post("agent", "finding", "PRIVATE_PAYLOAD_DO_NOT_EXPOSE")
    value = compact([post], width=width)
    assert "PRIVATE_PAYLOAD" not in value


@pytest.mark.parametrize("width", [float("nan"), float("inf"), object(), {}, 5_000_000])
def test_compact_invalid_or_huge_width_is_bounded(tmp_path, width):
    bridge = Bridge("compact-weird", tmp_path)
    post = bridge.post("agent", "finding", "X" * 4000)
    value = compact([post], width=width)
    assert len(value) < 5000


def test_compaction_under_six_simultaneous_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=6, mp_context=ctx) as pool:
        chunks = list(pool.map(_write_shared,
                 [(str(tmp_path), f"worker-{n}", 65) for n in range(6)]))
    assert len([v for chunk in chunks for v in chunk]) == 390
    journal = Bridge("process-compact-stress", tmp_path, limit=37)
    remaining = journal.posts()
    assert len(remaining) == 37
    assert len({p["id"] for p in remaining}) == 37
    produced = {v for chunk in chunks for v in chunk}
    assert {p["id"] for p in remaining} <= produced
    assert len(Bridge("process-compact-stress", tmp_path, limit=37).posts()) == 37
