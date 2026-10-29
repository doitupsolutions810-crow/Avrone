"""v0.21 robustness: check-then-act (TOCTOU) hammer tests for durable state (offline).

Each test releases N workers through a Barrier at once and repeats the race many times; the
thread switch interval is shrunk to make interleavings frequent. Rates measured before the fixes
in this round (same hammers, 200 iterations unless noted):

  * checkpoint Checkpoint.put: mutate+serialise+os.replace outside a lock; the replace order can
    differ from the serialise order, so the file lost a finished step, or json.dump raised an
    uncaught RuntimeError ("dictionary changed size during iteration") into the run 25/200 -> 0/200
  * agentpack.record: read-modify-write of the shared index under only the per-agent import lock
    (plus one shared temp name) dropped other agents' home records ......... 170/200 -> 0/200
  * evolve.record_history: read-modify-write of history.jsonl, shared temp name: a finished
    loop's row was lost ........................................................ 148/200 -> 0/200
  * jobqueue.JobQueue.claim: SELECT then UPDATE outside a transaction, so two queues on one file
    (two worker processes) both claimed the same job ........................... 69/100 -> 0/100
Proofs (no bug found; the hammer stays as a regression guard):
  * checkpoint.claim exclusivity, evolve_start background-loop cap, memstore cross-instance
    save/merge, agent-fs dir quota under concurrent writers, CodeX JSONL lines under two writers.
"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentfs_tools, agentpack, checkpoint, evolve, memstore
from avrone.codex import Codex
from avrone.jobqueue import JobQueue


@pytest.fixture(autouse=True)
def _fast_switch():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


def hammer(n, fn):
    """Run fn(i) for i in range(n) on n threads released together; returns results/exceptions."""
    barrier = threading.Barrier(n)
    out = [None] * n

    def work(i):
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as e:  # noqa: BLE001 - the test inspects it
            out[i] = e
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


# ------------------------------------------------------------------ checkpoint
def test_checkpoint_concurrent_puts_never_lose_a_step(tmp_path):
    for it in range(150):
        p = tmp_path / f"c{it}.json"
        ck = checkpoint.Checkpoint(p)
        sub = ck.scope("wave1.")
        res = hammer(8, lambda i: (sub if i % 2 else ck).put(f"worker.{i}", {"i": i}))
        assert not [r for r in res if isinstance(r, Exception)]
        steps = json.loads(p.read_text())["steps"]
        assert len(steps) == 8, f"iteration {it}: file lost a step: {sorted(steps)}"
        assert ck._store.write_errors == 0


def test_checkpoint_claim_is_exclusive(tmp_path):
    checkpoint._active.clear()
    try:
        for it in range(200):
            p = tmp_path / f"k{it}.json"
            res = hammer(8, lambda i: checkpoint.claim(p))
            winners = [r for r in res if callable(r)]
            assert len(winners) == 1, f"iteration {it}: {len(winners)} runs claimed one checkpoint"
            assert all(isinstance(r, checkpoint.CheckpointConflict) for r in res if not callable(r))
            winners[0]()
            assert not checkpoint.is_busy(p)
    finally:
        checkpoint._active.clear()


def test_checkpoint_stamp_owner_and_puts_keep_each_other(tmp_path):
    for it in range(100):
        p = tmp_path / f"o{it}.json"
        ck = checkpoint.Checkpoint(p)
        ck.put("meta", {"task": "t", "mode": "squad"})
        res = hammer(4, lambda i: checkpoint.stamp_owner(p, "fleet:a") if i == 0 else ck.put(f"w{i}", i))
        assert not [r for r in res if isinstance(r, Exception)]
        data = json.loads(p.read_text())
        assert data["steps"]["meta"]["task"] == "t"


# ------------------------------------------------------------------ agentpack index
def test_agentpack_index_records_of_different_agents_are_never_lost(tmp_path):
    for it in range(150):
        d = tmp_path / f"a{it}"
        res = hammer(8, lambda i: agentpack.record(d, f"agent{i}", home=f"node{i}", origin="x"))
        assert not [r for r in res if isinstance(r, Exception)], res
        idx = agentpack.load_index(d)
        assert sorted(idx) == sorted(f"agent{i}" for i in range(8)), f"iteration {it}: lost {idx.keys()}"
        assert all(idx[f"agent{i}"]["home"] == f"node{i}" for i in range(8))
        assert not list((d / agentpack.INDEX_DIR).glob("*.tmp"))


# ------------------------------------------------------------------ evolve
def _ectx(d: Path):
    return SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(d)),
                                                codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           name="Evolver-1", run_id="r", depth=0)


def test_evolve_history_keeps_every_concurrently_finished_loop(tmp_path):
    for it in range(150):
        ctx = _ectx(tmp_path / f"e{it}")
        hammer(8, lambda i: evolve.record_history(ctx, {"loop_id": f"L{i}", "goal": "g", "iterations": []}))
        rows = evolve.evolve_history(ctx)
        assert sorted(r["loop_id"] for r in rows) == [f"L{i}" for i in range(8)], f"iteration {it}"
        assert not [p for p in evolve._loops_dir(ctx).iterdir() if p.name.endswith(".tmp")]


def test_evolve_start_background_cap_holds_under_a_burst(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "2")

    async def go(it):
        ctx = _ectx(tmp_path / f"b{it}")
        gate = asyncio.Event()

        async def edits(i, prev):
            await gate.wait()
            return None  # "exhausted": the loop ends without cloning anything

        res = await asyncio.gather(*[evolve.evolve_start(ctx, "g", edits_fn=edits) for _ in range(6)])
        started = [r for r in res if r.get("loop_id")]
        assert len(started) == 2 and len(evolve._bg_running()) == 2
        assert all("already running" in r["error"] for r in res if not r.get("loop_id"))
        gate.set()
        for r in started:
            await evolve.evolve_wait(r["loop_id"], timeout=5)
        assert evolve._bg_running() == []

    evolve._BG.clear()
    try:
        for it in range(30):
            asyncio.run(go(it))
    finally:
        evolve._BG.clear()


# ------------------------------------------------------------------ job queue
def test_jobqueue_two_queues_on_one_file_never_double_claim(tmp_path):
    async def go(it):
        p = tmp_path / f"q{it}.db"
        qs = [JobQueue(p) for _ in range(4)]
        try:
            ids = [await qs[0].enqueue("k", {"n": n}) for n in range(5)]
            got = await asyncio.gather(*[q.claim() for q in qs for _ in range(2)])
            claimed = [g["id"] for g in got if g]
            assert len(claimed) == len(set(claimed)), f"iteration {it}: a job was claimed twice"
            assert sorted(claimed) == sorted(ids)  # 8 claims, 5 jobs: every job exactly once
            st = await qs[1].stats()
            assert st == {"running": 5}
            # fail() is a read-then-write too: concurrent fails of distinct jobs stay consistent
            outs = await asyncio.gather(*[qs[i % 4].fail(j, "x") for i, j in enumerate(claimed)])
            assert outs == ["requeued"] * 5
        finally:
            for q in qs:
                await q.close()

    for it in range(60):
        asyncio.run(go(it))


# ------------------------------------------------------------------ proofs: memstore, agent fs, codex
def test_memstore_two_instances_concurrent_save_and_merge_lose_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_NAMESPACES", "6")
    for it in range(40):
        p = tmp_path / f"m{it}" / "memory.jsonl"
        stores = [memstore.MemoryStore(p), memstore.MemoryStore(p)]

        def work(i):
            s = stores[i % 2]
            if i % 3 == 0:
                return s.merge(f"fleet:n{i}/a", [{"key": f"k{i}", "value": "v", "ts": 1.0}])
            return s.save(f"ns{i}", f"k{i}", "v")
        res = hammer(9, work)
        refused = [r for r in res if isinstance(r, memstore.MemoryRefused)]
        merge_rej = sum(r.get("rejected", 0) for r in res if isinstance(r, dict) and "merged" in r)
        assert not [r for r in res if isinstance(r, Exception) and not isinstance(r, memstore.MemoryRefused)]
        spaces = stores[0].namespaces()
        assert len(spaces) == 6  # the namespace cap is never exceeded by racing creators
        assert len(refused) + merge_rej == 3  # and exactly the over-cap creators were refused
        assert sum(len(v) for v in spaces.values()) == 6


def test_agent_fs_quota_never_exceeded_by_concurrent_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", str(10_000 / agentfs_tools.MB))  # ~10 KB
    events = []
    ctx = SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: events.append(a[1]))),
                          run_id="r", name="A-1", agent_id="a")
    quota = agentfs_tools.quota_bytes()
    for it in range(60):
        root = (tmp_path / f"r{it}").resolve()
        root.mkdir()
        res = hammer(8, lambda i: agentfs_tools.quota_write(ctx, root, root / f"f{i}", b"x" * 3000,
                                                            append=bool(i % 2)))
        ok = [r for r in res if isinstance(r, tuple)]
        assert len(ok) == quota // 3000, f"iteration {it}: {len(ok)} writes fit"
        assert agentfs_tools.dir_usage(root) <= quota
        assert all(isinstance(r, agentfs_tools.AgentFSError) for r in res if not isinstance(r, tuple))


def test_codex_lines_stay_whole_with_two_writers(tmp_path):
    for it in range(40):
        d = tmp_path / f"c{it}"
        cs = [Codex(d), Codex(d)]
        hammer(8, lambda i: [cs[i % 2].emit("run", "k", "a", blob="x" * 20000, i=i, j=j) for j in range(3)])
        evs = cs[0].replay("run")
        assert len(evs) == 24 and sorted((e["data"]["i"], e["data"]["j"]) for e in evs) == \
            sorted((i, j) for i in range(8) for j in range(3))
    assert os.path.isdir(tmp_path)
