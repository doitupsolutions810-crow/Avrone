"""Opt-in long evolve loops (AVRONE_EVOLVE_LONG=allow, hard-bounded) and restart handling of an
interrupted background loop (marked "interrupted" + resume hint + history, never auto-resumed). Offline."""
import asyncio
import json
import os

import pytest

from avrone import evolve
from tests.test_evolve_loop import BAD, GATE, GOOD, Clock, FakeRunner, env, mkctx  # noqa: F401


def run(c): return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AVRONE_EVOLVE_LONG", "AVRONE_EVOLVE_MAX_ITERS", "AVRONE_EVOLVE_TIME_BUDGET",
              "AVRONE_EVOLVE_HISTORY", "AVRONE_SELF_EVOLVE"):
        monkeypatch.delenv(k, raising=False)
    evolve._BG.clear()
    yield
    evolve._BG.clear()


# ---------------------------------------------------------------- long loops
def test_ceilings_are_constants():
    assert (evolve.MAX_ITERS_CAP, evolve.TIME_BUDGET_CAP_S) == (10, 1800)
    assert (evolve.LONG_MAX_ITERS_CAP, evolve.LONG_TIME_BUDGET_CAP_S) == (100, 43200)
    assert (evolve.DEFAULT_MAX_ITERS, evolve.DEFAULT_TIME_BUDGET_S) == (3, 600)


def test_default_off_keeps_existing_caps(monkeypatch):
    assert not evolve.long_loops_allowed()
    assert evolve.hard_caps() == (10, 1800)
    assert evolve.loop_limits() == (3, 600)
    assert evolve.loop_limits(10 ** 6, 10 ** 9) == (10, 1800)
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "40")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "5000")
    assert evolve.loop_limits(10 ** 6, 10 ** 9) == (10, 1800)


@pytest.mark.parametrize("val", ["", "0", "1", "yes", "true", "deny", "allow-ish", " allowed "])
def test_only_exact_allow_opts_in(monkeypatch, val):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", val)
    assert evolve.hard_caps() == (10, 1800)
    assert evolve.loop_limits(99, 10 ** 6) == (10, 1800)


@pytest.mark.parametrize("val", ["allow", "ALLOW", " Allow "])
def test_long_raises_ceiling_not_defaults(monkeypatch, val):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", val)
    assert evolve.hard_caps() == (100, 43200)
    assert evolve.loop_limits() == (3, 600)                    # defaults unchanged
    assert evolve.loop_limits(30, 4000) == (30, 4000)          # can now ask for more
    assert evolve.loop_limits(10 ** 6, float("inf")) == (100, 43200)   # but never past the long ceiling


def test_long_with_env_still_only_lowers(monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", "allow")
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "20")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "3000")
    assert evolve.loop_limits() == (20, 3000)
    assert evolve.loop_limits(45, 7000) == (20, 3000)
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "1e9")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "1e12")
    assert evolve.loop_limits(10 ** 6, 10 ** 9) == (100, 43200)


def test_long_loop_actually_runs_past_ten_iterations(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", "allow")
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "g", candidates=[BAD] * 11 + [GOOD], max_iters=12, test_cmd=GATE))
    assert r["max_iters"] == 12 and r["stopped"] == "accepted" and len(r["attempts"]) == 12


def test_without_opt_in_same_loop_stops_at_ten(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "g", candidates=[BAD] * 11 + [GOOD], max_iters=12, test_cmd=GATE))
    assert r["max_iters"] == 10 and r["stopped"] == "max_iters" and len(r["attempts"]) == 10


def test_long_time_budget_enforced(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", "allow")
    runner = FakeRunner(clock=env.clock, step=15000)
    monkeypatch.setattr(evolve, "_now", env.clock)
    ctx = mkctx(env.tmp, runner)
    r = run(evolve.evolve_loop(ctx, "g", candidates=[BAD] * 10, max_iters=10, time_budget_s=10 ** 6,
                               test_cmd=GATE))
    assert r["time_budget_s"] == 43200 and r["stopped"] == "time_budget"
    assert len(r["attempts"]) == 3      # 15000s each: stops after crossing 12 hours
    # the per-iteration gate timeout never exceeds 900s however long the budget is
    assert all(t is not None and t <= 900 for c, t in runner.cmds if "PYTHONPATH=" in c)


# ---------------------------------------------------------------- interrupted loops
def loops_dir(env):
    d = env.tmp / "d" / "evolve" / "loops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_loop(env, lid, **kw):
    m = {"loop_id": lid, "goal": "g " + lid, "by": "x", "ts": 1, "max_iters": 5, "time_budget_s": 600,
         "status": "running", "stopped": None, "accepted": None, "duration_s": 12.0, "mode": "background",
         "iterations": [{"iteration": 0, "pid": "p0", "outcome": "rejected", "passed": False}],
         "owner_pid": 2 ** 22 + 4321}
    m.update(kw)
    (loops_dir(env) / f"{lid}.json").write_text(json.dumps(m))
    return m


def read_loop(env, lid):
    return json.loads((loops_dir(env) / f"{lid}.json").read_text())


def test_dead_owner_marked_interrupted_on_disk_and_in_history(env):
    ctx = mkctx(env.tmp, FakeRunner())
    write_loop(env, "dead-1")
    (loops_dir(env) / "dead-1.cancel").write_text("1")
    assert evolve.reconcile_interrupted(ctx.core.settings) == ["dead-1"]
    m = read_loop(env, "dead-1")
    assert m["status"] == "interrupted" and m["stopped"] == "interrupted"
    assert "not resumed automatically" in m["resume_hint"] and "1/5" in m["resume_hint"]
    assert not (loops_dir(env) / "dead-1.cancel").exists()
    (h,) = evolve.evolve_history(ctx)
    assert h["loop_id"] == "dead-1" and h["status"] == "interrupted" and h["iterations"] == 1
    # idempotent: a second sweep changes nothing and does not duplicate history
    assert evolve.reconcile_interrupted(ctx.core.settings) == []
    assert len(evolve.evolve_history(ctx)) == 1


def test_status_tool_shows_resume_hint_and_cancel_is_noop(env):
    ctx = mkctx(env.tmp, FakeRunner())
    write_loop(env, "dead-2")
    out = run(evolve.t_evolve_status(ctx, {"loop_id": "dead-2"}))
    assert "dead-2 [interrupted]" in out and "resume:" in out and "evolve_start" in out
    assert evolve.evolve_cancel(ctx, "dead-2")["status"] == "interrupted"
    assert "dead-2 [interrupted/interrupted]" in run(evolve.t_evolve_history(ctx, {}))


def test_reused_pid_from_earlier_process_run_is_interrupted(env):
    ctx = mkctx(env.tmp, FakeRunner())
    write_loop(env, "pid1-old", owner_pid=os.getpid(), owner_token="an-earlier-process-run")
    assert evolve.evolve_status(ctx, "pid1-old")["status"] == "interrupted"
    assert read_loop(env, "pid1-old")["status"] == "interrupted"


@pytest.mark.parametrize("kw", [
    {"owner_pid": None},                                   # unknown owner: leave it
    {"owner_pid": os.getpid()},                            # legacy file of this process (no token)
    {"owner_pid": os.getpid(), "owner_token": evolve._PROC_TOKEN},   # ours, still running
    {"owner_pid": os.getppid()},                           # live foreign process
    {"status": "done", "stopped": "max_iters"},           # finished loops are never touched
    {"status": "cancelled", "stopped": "cancelled"},
])
def test_not_orphaned_left_alone(env, kw):
    ctx = mkctx(env.tmp, FakeRunner())
    before = write_loop(env, "keep", **kw)
    assert evolve.reconcile_interrupted(ctx.core.settings) == []
    assert read_loop(env, "keep") == before
    assert evolve.evolve_history(ctx) == []


def test_own_running_loops_get_token_and_are_not_interrupted(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        lid = (await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE))["loop_id"]
        m = read_loop(env, lid)
        assert m["owner_token"] == evolve._PROC_TOKEN and m["owner_pid"] == os.getpid()
        assert evolve.reconcile_interrupted(ctx.core.settings) == []
        res = await evolve.evolve_wait(lid, timeout=30)
        assert res["stopped"] == "accepted"
        assert read_loop(env, lid)["owner_token"] == evolve._PROC_TOKEN
    run(go())


def test_reconcile_tolerates_junk_and_missing_dir(env):
    ctx = mkctx(env.tmp, FakeRunner())
    assert evolve.reconcile_interrupted(ctx.core.settings) == []   # no loops dir yet
    (loops_dir(env) / "junk.json").write_text("{not json")
    (loops_dir(env) / "list.json").write_text("[1, 2]")
    (loops_dir(env) / "noid.json").write_text(json.dumps({"status": "running", "owner_pid": 2 ** 22 + 1}))
    write_loop(env, "dead-3")
    assert evolve.reconcile_interrupted(ctx.core.settings) == ["dead-3"]
    assert "dead-3" in run(evolve.t_evolve_list(ctx, {}))


def test_interrupted_loop_can_be_restarted_with_untried_candidates(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        write_loop(env, "dead-4")
        st = evolve.evolve_status(ctx, "dead-4")
        assert st["status"] == "interrupted" and st["iterations_done"] == 1
        lid = (await evolve.evolve_start(ctx, st["goal"], candidates=[GOOD], test_cmd=GATE))["loop_id"]
        res = await evolve.evolve_wait(lid, timeout=30)
        assert res["stopped"] == "accepted"
        return ctx, lid
    ctx, lid = run(go())
    ids = [h["loop_id"] for h in evolve.evolve_history(ctx)]
    assert ids == ["dead-4", lid]
