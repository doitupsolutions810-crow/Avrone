"""Background / resumable evolve loops. Offline: real git clones of a tiny temp repo, a FAKE gate
command (grep) -- never pytest-in-pytest -- and a fake clock."""
import asyncio
import json
import os

import pytest

from avrone import evolve
from tests.test_evolve_loop import BAD, GATE, GOOD, FakeRunner, env, mkctx, snapshot  # noqa: F401


def run(c): return asyncio.run(c)


class GatedRunner(FakeRunner):
    """Like FakeRunner, but every gate (test) run first waits for `release` so a test can watch the
    loop mid-iteration. `entered` is set when a gate run begins."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.release, self.entered, self.gates = asyncio.Event(), asyncio.Event(), 0

    async def run(self, cmd, timeout=None):
        if "PYTHONPATH=" in cmd:
            self.gates += 1
            self.entered.set()
            await self.release.wait()
        return await super().run(cmd, timeout)


@pytest.fixture(autouse=True)
def _clean_bg():
    evolve._BG.clear()
    yield
    evolve._BG.clear()


def loop_json(env, lid):
    return json.loads((env.tmp / "d" / "evolve" / "loops" / f"{lid}.json").read_text())


def test_start_returns_immediately_then_status_transitions(env):
    async def go():
        runner = GatedRunner()
        ctx = mkctx(env.tmp, runner)
        before = snapshot(env.repo)
        r = await evolve.evolve_start(ctx, "g", candidates=[BAD, GOOD], max_iters=5, test_cmd=GATE)
        lid = r["loop_id"]
        # returned before any work ran: no clone, no gate
        assert r["status"] == "running" and runner.cmds == []
        st = evolve.evolve_status(ctx, lid)
        assert st["status"] == "running" and st["iterations_done"] == 0 and st["live"]
        assert st["mode"] == "background"
        await asyncio.wait_for(runner.entered.wait(), 10)          # iteration 0 is now testing
        assert evolve.evolve_status(ctx, lid)["status"] == "running"
        runner.release.set()
        res = await evolve.evolve_wait(lid, timeout=30)
        assert res["stopped"] == "accepted" and res["status"] == "done"
        st = evolve.evolve_status(ctx, lid)
        assert st["status"] == "done" and st["iterations_done"] == 2 and st["accepted_count"] == 1
        assert not st["live"] and lid not in evolve._BG
        assert snapshot(env.repo) == before                         # live tree untouched
        return ctx, lid
    ctx, lid = run(go())
    out = run(evolve.t_evolve_status(ctx, {"loop_id": lid}))
    assert "[done/accepted]" in out


def test_cancel_stops_before_next_iteration(env):
    async def go():
        runner = GatedRunner()
        ctx = mkctx(env.tmp, runner)
        calls = []

        def fn(i, prev):
            calls.append(i)
            return BAD
        lid = (await evolve.evolve_start(ctx, "g", edits_fn=fn, max_iters=10, test_cmd=GATE))["loop_id"]
        await asyncio.wait_for(runner.entered.wait(), 10)          # mid iteration 0
        c = evolve.evolve_cancel(ctx, lid)
        assert c["status"] == "cancelling" and evolve.evolve_status(ctx, lid)["cancel_requested"]
        runner.release.set()
        res = await evolve.evolve_wait(lid, timeout=30)
        assert res["stopped"] == "cancelled" and res["status"] == "cancelled"
        assert calls == [0] and runner.gates == 1 and len(res["attempts"]) == 1   # #0 finished cleanly
        lp = loop_json(env, lid)
        assert lp["status"] == "cancelled" and len(lp["iterations"]) == 1
        assert not (env.tmp / "d" / "evolve" / "loops" / f"{lid}.cancel").exists()
        # cancelling a finished loop is a no-op
        assert evolve.evolve_cancel(ctx, lid)["status"] == "cancelled"
        assert evolve.evolve_cancel(ctx, "nope")["error"].startswith("ERROR")
    run(go())


def test_cancel_via_tool_and_cancel_raised_between_candidate_and_clone(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        holder = {}

        def fn(i, prev):
            if i == 1:  # cancel arrives while candidate #1 is being generated: it is never cloned/tested
                evolve.evolve_cancel(ctx, holder["lid"])
            return BAD
        holder["lid"] = (await evolve.evolve_start(ctx, "g", edits_fn=fn, max_iters=10, test_cmd=GATE))["loop_id"]
        res = await evolve.evolve_wait(holder["lid"], timeout=30)
        assert res["stopped"] == "cancelled" and len(res["attempts"]) == 1
        out = await evolve.t_evolve_cancel(ctx, {"loop_id": holder["lid"]})
        assert "nothing to cancel" in out
    run(go())


def test_background_caps_still_apply(env, monkeypatch):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        r = await evolve.evolve_start(ctx, "g", edits_fn=lambda i, p: BAD, max_iters=10_000,
                                      time_budget_s=10 ** 9, test_cmd="false")
        assert r["max_iters"] == evolve.MAX_ITERS_CAP and r["time_budget_s"] == evolve.TIME_BUDGET_CAP_S
        res = await evolve.evolve_wait(r["loop_id"], timeout=60)
        assert res["stopped"] == "max_iters" and len(res["attempts"]) == evolve.MAX_ITERS_CAP
    run(go())


def test_background_time_budget(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=40))
        r = await evolve.evolve_start(ctx, "g", edits_fn=lambda i, p: BAD, max_iters=10, time_budget_s=100,
                                      test_cmd=GATE)
        res = await evolve.evolve_wait(r["loop_id"], timeout=30)
        assert res["stopped"] == "time_budget" and len(res["attempts"]) == 3
    run(go())


def test_concurrent_background_loops_capped(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "1")
    assert evolve.max_bg_loops() == 1
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "999")
    assert evolve.max_bg_loops() == evolve.MAX_BG_LOOPS_CAP == 4
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "1")

    async def go():
        runner = GatedRunner()
        ctx = mkctx(env.tmp, runner)
        a = await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE)
        b = await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE)
        assert a["loop_id"] and b["loop_id"] is None and "already running" in b["error"]
        runner.release.set()
        await evolve.evolve_wait(a["loop_id"], timeout=30)
        c = await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE)   # slot freed
        assert c["loop_id"]
        await evolve.evolve_wait(c["loop_id"], timeout=30)
    run(go())


def test_deny_blocks_start_and_stops_running_loop(env, monkeypatch):
    async def go():
        runner = FakeRunner()
        ctx = mkctx(env.tmp, runner)
        monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
        r = await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE)
        assert r["loop_id"] is None and "disabled" in r["error"]
        assert "disabled" in await evolve.t_evolve_start(ctx, {"goal": "g", "candidates": [GOOD]})
        assert runner.cmds == [] and not evolve._BG
        assert evolve.list_loops(ctx.core.settings) == []
        monkeypatch.delenv("AVRONE_SELF_EVOLVE")

        def fn(i, prev):
            if i == 1:
                monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")    # operator flips the switch mid-run
            return BAD
        lid = (await evolve.evolve_start(ctx, "g", edits_fn=fn, max_iters=10, test_cmd=GATE))["loop_id"]
        res = await evolve.evolve_wait(lid, timeout=30)
        assert res["stopped"] == "denied" and len(res["attempts"]) == 1
    run(go())


def test_loops_survive_restart_and_orphans_show_interrupted(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        lid = (await evolve.evolve_start(ctx, "first goal", candidates=[GOOD], test_cmd=GATE))["loop_id"]
        await evolve.evolve_wait(lid, timeout=30)
        return ctx, lid
    ctx, lid = run(go())
    # "Restart": forget every in-process handle; the json on disk is all that's left.
    evolve._BG.clear()
    # A loop whose owning process died mid-run (dead pid) is reported as interrupted, not running.
    orphan = dict(loop_json(env, lid), loop_id="orphan-1", status="running", stopped=None,
                  owner_pid=2 ** 22 + 12345, ts=1)
    (env.tmp / "d" / "evolve" / "loops" / "orphan-1.json").write_text(json.dumps(orphan))
    ls = run(evolve.t_evolve_list(ctx, {}))
    assert f"{lid} [done/accepted]" in ls and "orphan-1 [interrupted]" in ls
    assert evolve.evolve_status(ctx, "orphan-1")["status"] == "interrupted"
    assert evolve.evolve_cancel(ctx, "orphan-1")["status"] == "interrupted"   # nothing to cancel
    # a running loop of a live foreign process stays "running" and is cancellable via the file
    live = dict(orphan, loop_id="live-1", owner_pid=os.getppid())
    (env.tmp / "d" / "evolve" / "loops" / "live-1.json").write_text(json.dumps(live))
    assert evolve.evolve_status(ctx, "live-1")["status"] == "running"
    assert evolve.evolve_cancel(ctx, "live-1")["status"] == "cancelling"
    assert (env.tmp / "d" / "evolve" / "loops" / "live-1.cancel").exists()


def test_cancel_file_honoured_by_foreground_loop(env):
    # cross-process cancel: the file alone stops a loop, whoever owns it
    ctx = mkctx(env.tmp, FakeRunner())

    def fn(i, prev):
        if i == 1:
            (lp,) = evolve.list_loops(ctx.core.settings)
            (env.tmp / "d" / "evolve" / "loops" / f"{lp['loop_id']}.cancel").write_text("x")
        return BAD
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=fn, max_iters=5, test_cmd=GATE))
    assert res["stopped"] == "cancelled" and len(res["attempts"]) == 1


def test_start_tool_wrapper_and_registration(env):
    async def go():
        ctx = mkctx(env.tmp, FakeRunner())
        assert (await evolve.t_evolve_start(ctx, {"goal": "g"})).startswith("ERROR")
        out = await evolve.t_evolve_start(ctx, {"goal": "g", "candidates": [GOOD], "test_cmd": GATE,
                                                "max_iters": 99, "time_budget_s": 10 ** 6})
        assert out.startswith("started background loop") and "10 iters" in out and "1800s" in out
        (lid,) = list(evolve._BG)
        await evolve.evolve_wait(lid, timeout=30)
        assert "background loop" not in await evolve.t_evolve_status(ctx, {"loop_id": lid})
    run(go())
    names = {n for n, *_ in evolve.EVOLVE_TOOLS}
    assert {"evolve_start", "evolve_cancel", "evolve_status", "evolve_list", "evolve_history"} <= names
