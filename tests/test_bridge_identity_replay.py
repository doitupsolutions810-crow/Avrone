"""Persistence isolation, collision resistance and replay first-write-wins.

These regressions protect the durable blackboard against hostile run identifiers
and contradictory journal records without changing valid historical run names.
"""
from __future__ import annotations

import json

import pytest

from avrone.bridge import Bridge, load_posts, safe_run_id


@pytest.mark.parametrize("a,b", [
    ("a/b", "ab"),
    ("a\\b", "ab"),
    ("..", "run"),
    ("/tmp/evil", "tmpevil"),
    ("a" * 81, "a" * 80),
    ("a\u00e9", "a"),
    ("a\x00b", "ab"),
    ("...good", "good"),
], ids=["slash", "backslash", "dot", "absolute", "truncate",
        "unicode", "nul", "leading-dot"])
def test_distinct_run_names_do_not_alias_journal(a, b):
    assert safe_run_id(a) != safe_run_id(b)
    for name in (a, b):
        safe = safe_run_id(name)
        assert len(safe) <= 80
        assert "/" not in safe and "\\" not in safe
        assert safe and not safe.startswith(".")


@pytest.mark.parametrize("unsafe,safe", [
    ("a/b", "ab"), ("..", "run"), ("/tmp/evil", "tmpevil"),
])
def test_distinct_run_journals_and_load_posts_stay_isolated(tmp_path, unsafe, safe):
    lhs = Bridge(unsafe, tmp_path)
    rhs = Bridge(safe, tmp_path)
    a = lhs.post("first", "finding", "do-not-cross-A")
    b = rhs.post("second", "finding", "do-not-cross-B")
    assert lhs.path != rhs.path
    assert [p["id"] for p in Bridge(unsafe, tmp_path).posts()] == [a["id"]]
    assert [p["id"] for p in Bridge(safe, tmp_path).posts()] == [b["id"]]
    assert [p["id"] for p in load_posts(tmp_path, unsafe)] == [a["id"]]
    assert [p["id"] for p in load_posts(tmp_path, safe)] == [b["id"]]


def test_duplicate_journal_record_cannot_overwrite_first_entry(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    first = {"id": "stable", "run_id": "journal", "agent": "trusted",
             "kind": "finding", "text": "first-trusted", "ts": 1, "refs": []}
    poisoned = dict(first, text="later-forged", agent="intruder", ts=2)
    unique = dict(first, id="unique", text="second-trusted", ts=3)
    path = folder / "journal.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in (first, poisoned, unique)))
    bridge = Bridge("journal", tmp_path, limit=8)
    assert [p["id"] for p in bridge.posts()] == ["stable", "unique"]
    assert bridge.get("stable")["text"] == "first-trusted"
    assert bridge.get("stable")["agent"] == "trusted"
    bridge.post("local", "conclusion", "fresh")
    assert Bridge("journal", tmp_path, limit=8).get("stable")["text"] == "first-trusted"


def test_duplicate_journal_line_does_not_replace_accepted_post_after_compaction(tmp_path):
    bridge = Bridge("compact", tmp_path, limit=2)
    first = bridge.post("trusted", "finding", "source")
    bridge.post("trusted", "finding", "fill")
    bridge.post("trusted", "finding", "tail")
    path = bridge.path
    before = len(Bridge("compact", tmp_path, limit=2).posts())
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(first, text="forged-resurrection")) + "\n")
    reloaded = Bridge("compact", tmp_path, limit=2)
    assert len(reloaded.posts()) == before
    assert reloaded.get(first["id"]) is None
