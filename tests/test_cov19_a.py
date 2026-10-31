"""Round 19 coverage: a background evolve task cancelled at the asyncio level (node shutdown, task
cancel) records its loop as cancelled instead of leaving it "running", and leaves _BG."""
import asyncio
import json

from avrone import evolve
from tests.test_evolve_loop import GATE, GOOD, FakeRunner, env, mkctx  # noqa: F401


def test_task_cancel_marks_loop_cancelled(env, monkeypatch):
    started = asyncio.Event

    async def go():
        gate = started()

        async def hang(*a, **k):
            gate.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(evolve, "evolve_loop", hang)
        ctx = mkctx(env.tmp, FakeRunner())
        r = await evolve.evolve_start(ctx, "g", candidates=[GOOD], test_cmd=GATE)
        lid = r["loop_id"]
        await gate.wait()
        task = evolve._BG[lid]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ctx, lid
    evolve._BG.clear()
    ctx, lid = asyncio.run(go())
    meta = json.loads(evolve._loop_path(ctx, lid).read_text())
    assert meta["status"] == "cancelled" and meta["stopped"] == "cancelled"
    assert lid not in evolve._BG
