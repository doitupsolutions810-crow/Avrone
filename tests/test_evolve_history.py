"""Bounded, restart-surviving history of finished evolve loops. Offline: tiny temp repo, fake grep gate."""
import asyncio
import json

import pytest

from avrone import evolve
from tests.test_evolve_loop import BAD, GATE, GOOD, FakeRunner, env, mkctx  # noqa: F401


def run(c): return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AVRONE_EVOLVE_HISTORY", raising=False)
    evolve._BG.clear()
    yield
    evolve._BG.clear()


def hist_file(env):
    return env.tmp / "d" / "evolve" / "loops" / "history.jsonl"


def test_history_cap_env_can_only_lower(monkeypatch):
    assert evolve.history_cap() == evolve.DEFAULT_HISTORY == 50
    monkeypatch.setenv("AVRONE_EVOLVE_HISTORY", "5")
    assert evolve.history_cap() == 5
    for raised in ("51", "500", "1e9", "inf"):
        monkeypatch.setenv("AVRONE_EVOLVE_HISTORY", raised)
        assert evolve.history_cap() == 50
    for junk in ("0", "-3", "nan", "abc", ""):
        monkeypatch.setenv("AVRONE_EVOLVE_HISTORY", junk)
        assert evolve.history_cap() == 50
    assert evolve.HISTORY_CAP == 200 and evolve.DEFAULT_HISTORY <= evolve.HISTORY_CAP


def test_finished_loop_is_recorded_and_survives_restart(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "fix value", candidates=[BAD, GOOD], test_cmd=GATE))
    assert r["stopped"] == "accepted"
    rows = evolve.evolve_history(ctx)
    assert len(rows) == 1
    h = rows[0]
    assert h["loop_id"] == r["loop_id"] and h["status"] == "done" and h["stopped"] == "accepted"
    assert h["iterations"] == 2 and h["accepted_count"] == 1 and h["accepted_pid"] == r["accepted"]["pid"]
    # a "restart": a fresh context on the same data dir still sees it
    assert evolve.evolve_history(mkctx(env.tmp, FakeRunner()))[0]["loop_id"] == r["loop_id"]
    # history.jsonl is not mistaken for a loop
    assert [lp["loop_id"] for lp in evolve.list_loops(ctx.core.settings)] == [r["loop_id"]]


def test_history_is_bounded_and_keeps_newest(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_HISTORY", "3")
    ctx = mkctx(env.tmp, FakeRunner())
    ids = [run(evolve.evolve_loop(ctx, f"g{i}", candidates=[BAD], max_iters=1, test_cmd=GATE))["loop_id"]
           for i in range(5)]
    rows = evolve.evolve_history(ctx)
    assert [h["loop_id"] for h in rows] == ids[-3:]
    assert len(hist_file(env).read_text().splitlines()) == 3
    assert all(h["stopped"] == "max_iters" and h["accepted_count"] == 0 for h in rows)
    assert [h["loop_id"] for h in evolve.evolve_history(ctx, limit=1)] == ids[-1:]
    assert len(evolve.evolve_history(ctx, limit=999)) == 3


def test_same_loop_recorded_once_and_goal_truncated(env):
    ctx = mkctx(env.tmp, FakeRunner())
    meta = {"loop_id": "L1", "goal": "x" * 1000, "status": "done", "iterations": []}
    evolve.record_history(ctx, meta)
    evolve.record_history(ctx, dict(meta, status="error", stopped="error: boom"))
    rows = evolve.evolve_history(ctx)
    assert len(rows) == 1 and rows[0]["status"] == "error"
    assert len(rows[0]["goal"]) == evolve.HISTORY_GOAL_CHARS


def test_corrupt_lines_skipped_and_write_failure_harmless(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    evolve.record_history(ctx, {"loop_id": "A", "goal": "a", "iterations": []})
    with hist_file(env).open("a") as f:
        f.write("{torn line\n[1,2]\n" + json.dumps({"no": "id"}) + "\n")
    evolve.record_history(ctx, {"loop_id": "B", "goal": "b", "iterations": []})
    assert [h["loop_id"] for h in evolve.evolve_history(ctx)] == ["A", "B"]

    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(evolve.os, "replace", boom)
    evolve.record_history(ctx, {"loop_id": "C", "goal": "c", "iterations": []})  # must not raise
    assert [h["loop_id"] for h in evolve.evolve_history(ctx)] == ["A", "B"]


def test_background_loop_and_cancel_recorded(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        r = await evolve.evolve_start(ctx, "bg", candidates=[BAD, GOOD], test_cmd=GATE)
        res = await evolve.evolve_wait(r["loop_id"], timeout=30)
        return ctx, r["loop_id"], res
    ctx, lid, res = run(go())
    h = evolve.evolve_history(ctx)[-1]
    assert h["loop_id"] == lid and h["mode"] == "background" and h["stopped"] == res["stopped"] == "accepted"


def test_crashed_background_loop_recorded_as_error(env, monkeypatch):
    async def explode(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(evolve, "_propose_once", explode)

    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        r = await evolve.evolve_start(ctx, "crash", candidates=[GOOD], test_cmd=GATE)
        res = await evolve.evolve_wait(r["loop_id"], timeout=30)
        return ctx, r["loop_id"], res
    ctx, lid, res = run(go())
    assert res["status"] == "error"
    h = evolve.evolve_history(ctx)[-1]
    assert h["loop_id"] == lid and h["status"] == "error" and "kaboom" in h["stopped"]


def test_existing_caps_unchanged(monkeypatch):
    assert evolve.MAX_ITERS_CAP == 10 and evolve.TIME_BUDGET_CAP_S == 1800
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "99")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "99999")
    assert evolve.loop_limits(50, 10 ** 6) == (10, 1800)
