"""Adversarial input-work bounds for the run bridge."""
from __future__ import annotations

import itertools

import pytest

from avrone.bridge import (
    Bridge, MAX_REF_SCAN, MAX_REMOTE_BATCH, MAX_REFS,
)


def test_infinite_references_are_not_eagerly_materialized(tmp_path):
    bridge = Bridge("infinite-refs", tmp_path)
    pulls = []

    def generator():
        for n in itertools.count():
            pulls.append(n)
            yield "../../not-a-post-id"

    posted = bridge.post("agent", "finding", "bounded generator", refs=generator())
    assert posted["refs"] == []
    assert len(pulls) == MAX_REF_SCAN


@pytest.mark.parametrize("bad_refs", [
    42, True, object(), {"strange": "dictionary"}, None,
])
def test_malformed_non_iterable_references_do_not_crash(tmp_path, bad_refs):
    bridge = Bridge("noniterable", tmp_path)
    post = bridge.post("agent", "finding", "safe", refs=bad_refs)
    assert isinstance(post["refs"], list)
    assert len(post["refs"]) <= MAX_REFS


def test_massive_reference_list_has_a_bounded_inspection_budget(tmp_path):
    bridge = Bridge("wide-refs", tmp_path)
    malformed = ["../bad"] * 25_000
    malformed.append("valid-but-beyond-budget")
    assert bridge.post("agent", "finding", "bounded", refs=malformed)["refs"] == []


def test_remote_batch_work_and_journal_are_bounded(tmp_path):
    bridge = Bridge("huge-fanin", tmp_path, limit=50)
    rows = [
        {"id": f"edge-{n}", "kind": "finding", "text": f"entry-{n}",
         "ts": n + 1, "agent": "peer", "refs": []}
        for n in range(MAX_REMOTE_BATCH + 7)
    ]
    stats = bridge.merge_remote(rows, "peer")
    assert stats == {"merged": MAX_REMOTE_BATCH, "skipped": 0, "rejected": 7}
    assert len(bridge.posts()) == 50
    reopened = Bridge("huge-fanin", tmp_path, limit=50)
    assert [p["id"] for p in reopened.posts()] == [f"edge-{n}" for n in range(MAX_REMOTE_BATCH - 50, MAX_REMOTE_BATCH)]


def test_remote_batch_keeps_first_id_and_reports_duplicates(tmp_path):
    bridge = Bridge("remote-first", tmp_path)
    rows = [
        {"id": "same", "kind": "finding", "text": "first"},
        {"id": "same", "kind": "finding", "text": "forged"},
    ]
    assert bridge.merge_remote(rows, "peer") == {"merged": 1, "skipped": 1, "rejected": 0}
    assert Bridge("remote-first", tmp_path).get("same")["text"] == "first"
