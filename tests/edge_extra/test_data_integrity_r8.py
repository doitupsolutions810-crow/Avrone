"""Round 8: data integrity — mixed valid+poison lines keep valid ones in order,
concurrent appenders do not interleave-corrupt a line, and a large log honors the cap.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from avrone.bridge import Bridge, blackboard_accept, blackboard_posts, normalize_blackboard_post
from avrone.codex import Codex, iter_lines, replay_limit
from avrone.config import Settings


def test_codex_mixed_poison_preserves_valid_in_order(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    path = c.log_dir / "mix.jsonl"
    raw = [
        "not-json",
        json.dumps({"ts": 1, "run_id": "mix", "kind": "a", "agent": "x", "data": {"n": 1}}),
        "",
        "{",
        json.dumps({"ts": 2, "run_id": "mix", "kind": "b", "agent": "x", "data": {"n": 2}}),
        "[1, 2, 3]",
        json.dumps({"ts": 3, "run_id": "mix", "kind": "c", "agent": "x", "data": {"n": 3}}),
        "null",
    ]
    path.write_text("\n".join(raw) + "\n")
    evs = c.replay("mix")
    kinds = [e.get("kind") or e.get("data", {}).get("n") for e in evs]
    # Only the three valid objects, in file order.
    ns = [e.get("data", {}).get("n") for e in evs]
    assert ns == [1, 2, 3]
    assert all(isinstance(e, dict) for e in evs)


def test_codex_overlong_and_valid_keep_order(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    path = c.log_dir / "cap.jsonl"
    valid1 = json.dumps({"ts": 1, "run_id": "cap", "kind": "a", "agent": "x", "data": {"n": 1}})
    valid2 = json.dumps({"ts": 2, "run_id": "cap", "kind": "b", "agent": "x", "data": {"n": 2}})
    # An overlong line (no newline until way past a small cap) is skipped by iter_lines
    # when we feed the parser a tight max_line; replay uses MAX_LINE_BYTES, so instead
    # drop a truncated (invalid JSON) mid-file and confirm neighbors stay ordered.
    path.write_text(valid1 + "\n" + valid1[:20] + "\n" + valid2 + "\n")
    evs = c.replay("cap")
    assert [e["data"]["n"] for e in evs] == [1, 2]


def test_concurrent_codex_emitters_do_not_corrupt_lines(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    n_threads, n_each = 12, 40
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(n_each):
                c.emit("race", "evt", f"t{tid}", i=i, tid=tid, blob="x" * 120)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert errors == []
    path = c.log_dir / "race.jsonl"
    lines = path.read_text().splitlines()
    evs = []
    for line in lines:
        evs.append(json.loads(line))  # must be one complete object per line
    assert len(evs) == n_threads * n_each
    seen = {(e["data"]["tid"], e["data"]["i"]) for e in evs}
    assert len(seen) == n_threads * n_each


def test_bridge_cap_keeps_newest_and_file_is_valid_jsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "5")
    # Re-import the cap helper's env read: Bridge reads the cap at post time via a function.
    b = Bridge("caprun", tmp_path)
    ids = [b.post("ag", "finding", f"n{i}")["id"] for i in range(12)]
    kept = [p["id"] for p in b.posts()]
    assert kept == ids[-5:]
    # On-disk lines (after compact) are all valid JSON objects.
    path = tmp_path / "bridge" / "caprun.jsonl"
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        assert isinstance(rec, dict)


def test_blackboard_mixed_poison_on_disk_keeps_valid_order(tmp_path: Path) -> None:
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), fleet=None, codex=None)
    ids = []
    for i in range(3):
        post, err = normalize_blackboard_post({
            "id": f"p{i}", "kind": "finding", "text": f"ok-{i}", "run_id": "mix",
        })
        assert err is None and post is not None
        blackboard_accept(core, post)
        ids.append(f"p{i}")
    # Inject poison into the run's jsonl beside the valid records.
    path = tmp_path / "bridge" / "mix.jsonl"
    if path.is_file():
        body = path.read_text()
        path.write_text("not-json\n" + body + "[1]\n" + json.dumps({"id": "??"}) + "\n")
    got = [p["id"] for p in blackboard_posts(core)]
    # Valid posts stay in their original order; poison is ignored.
    assert got == ids
