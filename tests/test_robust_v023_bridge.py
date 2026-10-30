"""v0.23 robustness: the conclude Bridge, its cross-node relay, and the lessons loop.

Each test pins one bug found in review of avrone/bridge.py (torn-tail append, merge crash on
unserialisable posts, evicted posts resurrected by relay, non-finite remote ts, two instances
on one file losing posts at compaction, non-atomic distill_lessons) plus concurrency hammers.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading

import pytest

from avrone import bridge as bm


def _bridge(tmp_path, run="r1", node="local", limit=None):
    return bm.Bridge(run, data_dir=tmp_path, node_id=node, limit=limit)


def _remote(pid, text="t", kind="finding", ts=1.0, origin="nodeB"):
    return {"id": pid, "kind": kind, "text": text, "refs": [], "ts": ts, "origin_node": origin, "agent": "a"}


# ------------------------------------------------------------------ bug 1: torn tail
def test_append_after_torn_tail_survives_reload(tmp_path):
    b = _bridge(tmp_path)
    b.post("a", "finding", "first")
    with b.path.open("a", encoding="utf-8") as f:
        f.write('{"id": "torn", "kind": "fin')  # crash mid-write: no newline
    b2 = _bridge(tmp_path)
    p = b2.post("a", "finding", "second")
    b3 = _bridge(tmp_path)
    texts = [x["text"] for x in b3.posts()]
    assert texts == ["first", "second"]
    assert b3.get(p["id"]) is not None


# ------------------------------------------------------------------ bug 2: merge crash
def test_merge_remote_rejects_unserialisable_posts(tmp_path):
    b = _bridge(tmp_path)
    loop = _remote("loop1")
    loop["self"] = loop  # circular -> json.dumps ValueError
    deep: dict = _remote("deep1")
    cur = deep
    for _ in range(5000):
        cur["x"] = {}
        cur = cur["x"]
    stats = b.merge_remote([loop, deep, _remote("ok1")], "nodeB")  # must not raise
    assert stats["rejected"] >= 1 and stats["merged"] + stats["rejected"] == 3
    assert b.get("loop1") is None and b.get("ok1") is not None
    assert set(b.get("ok1")) == {"id", "run_id", "agent", "role", "kind", "text", "refs", "ts", "origin_node"}


# ------------------------------------------------------------------ bug 3: resurrection
def test_evicted_post_not_resurrected_by_relay(tmp_path):
    a = _bridge(tmp_path / "A", limit=3, node="A")
    first = a.post("x", "finding", "p0")
    exported = a.export()
    for i in range(1, 4):
        a.post("x", "finding", f"p{i}")
    assert a.get(first["id"]) is None  # evicted
    stats = a.merge_remote(exported, "B")  # B relays back what it got earlier
    assert stats["merged"] == 0
    assert [p["text"] for p in a.posts()] == ["p1", "p2", "p3"]


def test_eviction_tombstones_survive_reload(tmp_path):
    a = _bridge(tmp_path, limit=2)
    old = a.post("x", "finding", "old")
    snap = a.export()
    for i in range(6):  # forces eviction and compaction
        a.post("x", "finding", f"n{i}")
    a2 = _bridge(tmp_path, limit=2)
    assert a2.merge_remote(snap, "B")["merged"] == 0
    assert a2.get(old["id"]) is None


# ------------------------------------------------------------------ bug 4: ts
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "12"])
def test_merge_remote_sanitises_ts(tmp_path, bad):
    b = _bridge(tmp_path)
    b.merge_remote([_remote("t1", ts=bad)], "nodeB")
    ts = b.get("t1")["ts"]
    assert isinstance(ts, float) and math.isfinite(ts) and ts > 1e9
    # still loads back
    assert _bridge(tmp_path).get("t1") is not None


# ------------------------------------------------------------------ bug 5: two instances
def test_two_instances_one_file_keep_all_posts_through_compaction(tmp_path):
    a = _bridge(tmp_path, limit=50)
    b = _bridge(tmp_path, limit=50)
    ids = []
    for i in range(120):  # each instance alone passes its 2*limit compaction threshold
        ids.append(a.post("a", "finding", f"a{i}")["id"])
        ids.append(b.post("b", "finding", f"b{i}")["id"])
    fresh = _bridge(tmp_path, limit=50)
    have = {p["id"] for p in fresh.posts()}
    # ring keeps the newest 50 of the 240 posts made across both instances
    assert len(have) == 50
    assert set(ids[-50:]) == have


# ------------------------------------------------------------------ bug 6: distill race
def test_concurrent_distill_reports_each_lesson_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    b = _bridge(tmp_path)
    for i in range(5):
        b.post("a", "conclusion", f"lesson number {i} about caching layers")
    results: list[list[str]] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(bm.distill_lessons(tmp_path, b, "task", k=5))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    flat = [k for r in results for k in r]
    assert len(flat) == len(set(flat)) == 5


# ------------------------------------------------------------------ hammers
def test_thread_hammer_posts_unique_ring_exact_and_parseable(tmp_path):
    b = _bridge(tmp_path, limit=100)
    n_threads, per = 8, 40

    def go(t):
        for i in range(per):
            b.post(f"a{t}", "finding", f"{t}-{i}")

    ts = [threading.Thread(target=go, args=(t,)) for t in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(b) == 100
    for line in b.path.read_text().splitlines():
        if line.strip():
            json.loads(line)
    assert len(_bridge(tmp_path, limit=100)) == 100


def test_async_gather_posts_and_reads(tmp_path):
    b = _bridge(tmp_path, limit=1000)

    async def writer(t):
        for i in range(25):
            await asyncio.to_thread(b.post, f"a{t}", "finding", f"{t}-{i}")

    async def reader():
        for _ in range(50):
            ps = await asyncio.to_thread(b.posts)
            assert len({p["id"] for p in ps}) == len(ps)

    async def main():
        await asyncio.gather(*(writer(t) for t in range(8)), reader(), reader())

    asyncio.run(main())
    assert len(b) == 200
    assert len(_bridge(tmp_path, limit=1000)) == 200


def test_concurrent_merge_from_two_nodes_dedupes(tmp_path):
    b = _bridge(tmp_path, limit=1000)
    local = b.post("me", "finding", "mine")
    shared = [_remote(f"s{i}", origin="B") for i in range(60)]
    overwrite = dict(_remote(local["id"], text="HIJACK"))
    barrier = threading.Barrier(2)
    out = []

    def go(origin, batch):
        barrier.wait()
        out.append(b.merge_remote(batch, origin))

    t1 = threading.Thread(target=go, args=("B", shared + [overwrite]))
    t2 = threading.Thread(target=go, args=("C", list(reversed(shared))))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sum(s["merged"] for s in out) == 60
    assert b.get(local["id"])["text"] == "mine"
    assert all(b.get(f"s{i}")["origin_node"] == "B" for i in range(60))


def test_relay_cycle_converges(tmp_path):
    a = _bridge(tmp_path / "A", node="A", limit=50)
    c = _bridge(tmp_path / "C", node="C", limit=50)
    for i in range(10):
        a.post("x", "finding", f"a{i}")
        c.post("y", "finding", f"c{i}")
    for _ in range(5):
        c.merge_remote(a.export(), "A")
        a.merge_remote(c.export(), "C")
    assert len(a) == len(c) == 20
    assert {p["id"] for p in a.posts()} == {p["id"] for p in c.posts()}
    assert c.merge_remote(a.export(), "A")["merged"] == 0
