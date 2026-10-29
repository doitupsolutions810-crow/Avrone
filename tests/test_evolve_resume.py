"""Resumable evolve loops: per-iteration checkpoints + evolve_start resume_loop_id. Offline: real git
clones of a tiny temp repo, a fake grep gate (never pytest-in-pytest) and a fake clock."""
import asyncio
import json
import os

import pytest

from avrone import evolve
from tests.test_evolve_loop import BAD, GATE, GOOD, Clock, FakeRunner, env, mkctx  # noqa: F401


def run(c): return asyncio.run(c)


class Crash(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AVRONE_EVOLVE_LONG", "AVRONE_EVOLVE_MAX_ITERS", "AVRONE_EVOLVE_TIME_BUDGET",
              "AVRONE_EVOLVE_HISTORY", "AVRONE_SELF_EVOLVE", "AVRONE_EVOLVE_MAX_BG"):
        monkeypatch.delenv(k, raising=False)
    evolve._BG.clear()
    yield
    evolve._BG.clear()


def loops_dir(env):
    d = env.tmp / "d" / "evolve" / "loops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_loop(env, lid):
    return json.loads((loops_dir(env) / f"{lid}.json").read_text())


def write_raw(env, lid, m):
    (loops_dir(env) / f"{lid}.json").write_text(json.dumps(m))


def crash_at(monkeypatch, iteration):
    """The process 'dies' when the loop starts iteration `iteration` (after earlier ones completed)."""
    real = evolve._propose_once

    async def wrapped(ctx, goal, edits, test_cmd=None, timeout=900, extra_meta=None, confine_to=None):
        if (extra_meta or {}).get("iteration") == iteration:
            raise Crash("power cut")
        return await real(ctx, goal, edits, test_cmd, timeout, extra_meta, confine_to)
    monkeypatch.setattr(evolve, "_propose_once", wrapped)
    return real


def interrupted_loop(env, monkeypatch, ctx, candidates, at, **kw):
    """Run a foreground loop that crashes at iteration `at`, then make it look like a restart happened."""
    real = crash_at(monkeypatch, at)
    with pytest.raises(Crash):
        run(evolve.evolve_loop(ctx, "fix it", candidates=candidates, test_cmd=GATE, loop_id="L1", **kw))
    monkeypatch.setattr(evolve, "_propose_once", real)
    m = read_loop(env, "L1")
    assert m["status"] == "running" and len(m["iterations"]) == at
    m["owner_token"] = "an-earlier-process-run"        # same pid, earlier process: orphaned
    write_raw(env, "L1", m)
    assert evolve.evolve_status(ctx, "L1")["status"] == "interrupted"
    return m


def resume(ctx, lid="L1", **kw):
    async def go():
        r = await evolve.evolve_start(ctx, "", resume_loop_id=lid, **kw)
        if r.get("error"):
            return r, None
        return r, await evolve.evolve_wait(lid, timeout=60)
    return run(go())


# ---------------------------------------------------------------- checkpoint
def test_every_iteration_checkpoints(env):
    ctx = mkctx(env.tmp, FakeRunner())
    run(evolve.evolve_loop(ctx, "g", candidates=[BAD, BAD], max_iters=2, test_cmd=GATE, loop_id="C1"))
    ck = read_loop(env, "C1")["checkpoint"]
    assert ck["next_iteration"] == 2 and ck["test_cmd"] == GATE and ck["candidates_stored"]
    assert ck["candidates"] == [BAD, BAD]


def test_oversized_candidates_not_stored(env, monkeypatch):
    monkeypatch.setattr(evolve, "CHECKPOINT_MAX_BYTES", 10)
    ctx = mkctx(env.tmp, FakeRunner())
    run(evolve.evolve_loop(ctx, "g", candidates=[BAD], max_iters=1, test_cmd=GATE, loop_id="C2"))
    ck = read_loop(env, "C2")["checkpoint"]
    assert ck["candidates"] is None and not ck["candidates_stored"]


def test_resume_hint_names_resume_loop_id(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, BAD, GOOD], at=1, max_iters=3)
    hint = read_loop(env, "L1")["resume_hint"]
    assert "not resumed automatically" in hint and '"resume_loop_id": "L1"' in hint and "iteration 1" in hint


# ---------------------------------------------------------------- resume continues counting
def test_resume_continues_from_last_completed_iteration(env, monkeypatch):
    events = []
    ctx = mkctx(env.tmp, FakeRunner(), events)
    interrupted_loop(env, monkeypatch, ctx, [BAD, BAD, BAD, GOOD], at=2, max_iters=5)
    r, res = resume(ctx)
    assert r["resumed_from"] == 2 and r["loop_id"] == "L1" and r["max_iters"] == 5 and r["resumed"] == 1
    assert res["stopped"] == "accepted" and res["resumed_from"] == 2 and res["iterations_total"] == 4
    assert [a["iteration"] for a in res["attempts"]] == [2, 3]
    m = read_loop(env, "L1")
    assert [i["iteration"] for i in m["iterations"]] == [0, 1, 2, 3]
    assert [i["outcome"] for i in m["iterations"]] == ["rejected"] * 3 + ["accepted"]
    assert m["status"] == "done" and m["resumed"] == 1 and "resume_hint" not in m
    assert m["checkpoint"]["next_iteration"] == 4 and m["owner_token"] == evolve._PROC_TOKEN
    (h,) = evolve.evolve_history(ctx)              # the interrupted row was replaced, not duplicated
    assert h["loop_id"] == "L1" and h["status"] == "done" and h["iterations"] == 4
    (ev,) = [d for k, d in events if k == "evolve.resume"]
    assert ev["loop_id"] == "L1" and ev["from_iteration"] == 2 and ev["max_iters"] == 5


def test_resume_twice(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 4 + [GOOD], at=1, max_iters=6)
    real = crash_at(monkeypatch, 3)
    r, _ = run(_start_and_swallow(ctx))
    monkeypatch.setattr(evolve, "_propose_once", real)
    m = read_loop(env, "L1")
    assert m["status"] == "error"            # a crash of a resumed background task is recorded, not stuck
    m.update(status="running", stopped=None, owner_token="earlier")   # pretend it was a restart instead
    write_raw(env, "L1", m)
    r, res = resume(ctx)
    assert r["resumed_from"] == 3 and r["resumed"] == 2
    assert res["stopped"] == "accepted" and res["iterations_total"] == 5


async def _start_and_swallow(ctx):
    r = await evolve.evolve_start(ctx, "", resume_loop_id="L1")
    return r, await evolve.evolve_wait("L1", timeout=60)


def test_resume_with_explicit_candidates_when_none_stored(env, monkeypatch):
    monkeypatch.setattr(evolve, "CHECKPOINT_MAX_BYTES", 10)
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, BAD, GOOD], at=2, max_iters=3)
    r, _ = resume(ctx)
    assert "no checkpointed candidates" in r["error"]
    assert read_loop(env, "L1")["status"] == "interrupted"
    r, _ = resume(ctx, candidates=[[{"path": "x"}], "junk"])
    assert "non-empty edit lists" in r["error"]
    r, res = resume(ctx, candidates=[BAD, BAD, GOOD])      # full list: iteration i uses candidates[i]
    assert res["stopped"] == "accepted" and [a["iteration"] for a in res["attempts"]] == [2]


# ---------------------------------------------------------------- caps across resume
def test_iteration_cap_counts_earlier_iterations(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 6, at=2, max_iters=3)
    r, res = resume(ctx, max_iters=99)                     # asking for more never raises the cap
    assert r["max_iters"] == 3
    assert res["stopped"] == "max_iters" and len(res["attempts"]) == 1 and res["iterations_total"] == 3
    assert len(read_loop(env, "L1")["iterations"]) == 3


def test_resume_can_lower_caps(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 6, at=1, max_iters=6)
    r, res = resume(ctx, max_iters=2)
    assert r["max_iters"] == 2 and res["iterations_total"] == 2 and res["stopped"] == "max_iters"


def test_time_budget_counts_earlier_time(env, monkeypatch):
    runner = FakeRunner(clock=env.clock, step=100)
    ctx = mkctx(env.tmp, runner)
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 6, at=2, max_iters=6, time_budget_s=250)
    assert read_loop(env, "L1")["checkpoint"]["elapsed_s"] == pytest.approx(200)
    env.clock.t = 10_000.0          # wall/monotonic time jumps across the restart: irrelevant to the budget
    r, res = resume(ctx, time_budget_s=10 ** 6)
    assert r["time_budget_s"] == 250 and r["elapsed_s"] == pytest.approx(200)
    assert res["stopped"] == "time_budget" and len(res["attempts"]) == 1 and res["iterations_total"] == 3
    # the resumed gate's timeout was bounded by the remaining 50s, not a fresh budget
    assert runner.cmds and [t for c, t in runner.cmds if "PYTHONPATH=" in c][-1] <= 50


def test_spent_caps_refused(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 6, at=2, max_iters=6)
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "2")      # operator lowered the ceiling since
    r, _ = resume(ctx)
    assert "iteration cap" in r["error"] and "2/2" in r["error"]
    monkeypatch.delenv("AVRONE_EVOLVE_MAX_ITERS")
    m = read_loop(env, "L1")
    m["duration_s"] = 600.0
    write_raw(env, "L1", m)
    r, _ = resume(ctx)
    assert "time budget" in r["error"]
    assert read_loop(env, "L1")["status"] == "interrupted"


def test_long_cap_withdrawn_clamps_resume(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", "allow")
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 20, at=2, max_iters=20)
    assert read_loop(env, "L1")["max_iters"] == 20
    monkeypatch.delenv("AVRONE_EVOLVE_LONG")
    r, res = resume(ctx)
    assert r["max_iters"] == 10 and res["iterations_total"] == 10


def test_junk_caps_in_file_fall_back_safely(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD] * 20, at=1, max_iters=5)
    m = read_loop(env, "L1")
    m.update(max_iters=10 ** 9, time_budget_s="inf", duration_s="junk")
    write_raw(env, "L1", m)
    r, _ = resume(ctx)
    assert r["max_iters"] == 10 and r["time_budget_s"] <= 1800      # hard caps still bound a tampered file


def test_background_cap_applies_to_resume(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "1")
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, GOOD], at=1, max_iters=2)

    async def go():
        blocker = asyncio.Event()

        async def fn(i, prev):
            await blocker.wait()
            return None
        other = await evolve.evolve_start(ctx, "other", edits_fn=fn, test_cmd=GATE)
        r = await evolve.evolve_start(ctx, "", resume_loop_id="L1")
        blocker.set()
        await evolve.evolve_wait(other["loop_id"], timeout=30)
        return r
    r = run(go())
    assert "already running" in r["error"]
    assert read_loop(env, "L1")["status"] == "interrupted"


# ---------------------------------------------------------------- refusals
def test_unknown_loop_refused(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r, _ = resume(ctx, lid="nope")
    assert "no loop nope" in r["error"]
    r, _ = resume(ctx, lid="")
    assert "no loop" in r["error"]


@pytest.mark.parametrize("status", ["done", "cancelled", "error"])
def test_finished_loops_refused(env, status):
    ctx = mkctx(env.tmp, FakeRunner())
    run(evolve.evolve_loop(ctx, "g", candidates=[BAD], max_iters=1, test_cmd=GATE, loop_id="F1"))
    m = read_loop(env, "F1")
    m["status"] = status
    write_raw(env, "F1", m)
    r, _ = resume(ctx, lid="F1")
    assert f"is {status}" in r["error"] and "only an interrupted loop" in r["error"]
    assert read_loop(env, "F1") == m


def test_running_loop_refused_and_double_resume_refused(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, BAD, GOOD], at=1, max_iters=3)

    async def go():
        gate = asyncio.Event()
        real = evolve._propose_once

        async def slow(*a, **k):
            await gate.wait()
            return await real(*a, **k)
        monkeypatch.setattr(evolve, "_propose_once", slow)
        first = await evolve.evolve_start(ctx, "", resume_loop_id="L1")
        second = await evolve.evolve_start(ctx, "", resume_loop_id="L1")
        assert evolve.evolve_status(ctx, "L1")["status"] == "running"
        gate.set()
        res = await evolve.evolve_wait("L1", timeout=60)
        return first, second, res
    first, second, res = run(go())
    assert first["resumed_from"] == 1 and "is running" in second["error"]
    assert res["stopped"] == "accepted"


def test_loop_with_accepted_iteration_refused(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, BAD], at=1, max_iters=3)
    m = read_loop(env, "L1")
    m["iterations"][0].update(outcome="accepted", passed=True)
    write_raw(env, "L1", m)
    r, _ = resume(ctx)
    assert "already has an accepted proposal" in r["error"]


def test_deny_refuses_resume(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, GOOD], at=1, max_iters=2)
    before = read_loop(env, "L1")
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    r, _ = resume(ctx)
    assert r["error"] == evolve.DENIED_MSG
    assert run(evolve.t_evolve_start(ctx, {"resume_loop_id": "L1"})) == evolve.DENIED_MSG
    assert read_loop(env, "L1") == before and evolve._BG == {}


def test_deny_flipped_after_resume_keeps_loop_resumable(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, GOOD], at=1, max_iters=2)

    async def go():
        r = await evolve.evolve_start(ctx, "", resume_loop_id="L1")
        os.environ["AVRONE_SELF_EVOLVE"] = "deny"      # flipped before the task's first step runs
        try:
            return r, await evolve.evolve_wait("L1", timeout=30)
        finally:
            os.environ.pop("AVRONE_SELF_EVOLVE", None)
    r, res = run(go())
    assert res["stopped"] == "denied"
    m = read_loop(env, "L1")
    assert m["status"] == "interrupted" and len(m["iterations"]) == 1
    r, res = resume(ctx)
    assert res["stopped"] == "accepted" and res["iterations_total"] == 2


# ---------------------------------------------------------------- tool wrapper
def test_tool_resume(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    interrupted_loop(env, monkeypatch, ctx, [BAD, GOOD], at=1, max_iters=2)

    async def go():
        out = await evolve.t_evolve_start(ctx, {"resume_loop_id": "L1"})
        res = await evolve.evolve_wait("L1", timeout=30)
        return out, res
    out, res = run(go())
    assert "resumed loop L1" in out and "from iteration 1" in out and "2 iters" in out
    assert res["stopped"] == "accepted"
    assert "is done" in run(evolve.t_evolve_start(ctx, {"resume_loop_id": "L1"}))
    assert "must be a loop id" in run(evolve.t_evolve_start(ctx, {"resume_loop_id": 5}))
    assert "must be a loop id" in run(evolve.t_evolve_start(ctx, {"resume_loop_id": "  "}))
    from avrone.tools import builtin_registry
    from avrone.tools_extra import register_extra_tools
    reg = builtin_registry()
    register_extra_tools(reg)
    t = reg.get("evolve_start")
    assert "resume_loop_id" in t.args_doc and "resume_loop_id" in t.description
