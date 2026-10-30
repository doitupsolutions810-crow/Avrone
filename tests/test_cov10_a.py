"""Round 10 coverage: trinity's fleet-facing fallbacks. Every remote failure must degrade to a
local run (never a crash), and a misbehaving fleet object must leave the squad local."""
import asyncio
from types import SimpleNamespace

import pytest

from avrone import trinity
from avrone.trinity import TrinityCore, _normalize_subtasks


def run(c):
    return asyncio.run(c)


def bare(fleet=None, governor=None):
    core = TrinityCore.__new__(TrinityCore)
    core.fleet = fleet
    core.fleet_hops = 0
    core.fleet_privileged = ""
    core.governor = governor or SimpleNamespace(target=lambda n: n, last_reason="")
    core.codex = SimpleNamespace(emit=lambda *a, **k: None)
    return core


class Boom:
    enabled = True

    def __getattr__(self, name):
        def f(*a, **k):
            raise RuntimeError(name)
        return f


@pytest.mark.parametrize("reply", ["no json", '{"subtasks": "nope"}', "[1, 2]", '{"subtasks": [1, {"role": "x"}]}',
                                   "{" * 3000 + "}" * 3000])
def test_normalize_subtasks_junk_falls_back(reply):
    subs = _normalize_subtasks(reply, 2)
    assert len(subs) == 2 and all(s.get("focus") for s in subs)


def test_worker_limit_governor_error_and_throttle():
    c = bare(governor=SimpleNamespace(target=lambda n: (_ for _ in ()).throw(RuntimeError())))
    assert c._worker_limit("r", 4) == 4
    seen = []
    c = bare(governor=SimpleNamespace(target=lambda n: 0, last_reason="hot"))
    c.codex = SimpleNamespace(emit=lambda *a, **k: seen.append(a[1]))
    assert c._worker_limit("r", 4) == 1 and seen == ["governor.throttle"]


def test_offload_plan_bad_fleet_stays_local():
    assert bare(Boom())._offload_plan("r", 3) is None


def test_offload_plan_plan_error_means_no_targets():
    fleet = SimpleNamespace(enabled=True, offload_mode=lambda: "spill",
                            offload_plan=lambda *a: (_ for _ in ()).throw(RuntimeError()))
    c = bare(fleet, SimpleNamespace(target=lambda n: 1, last_reason=""))
    assert c._offload_plan("r", 3) == (1, {})


def test_offload_plan_privileged_or_single_never_leaves():
    fleet = SimpleNamespace(enabled=True, offload_mode=lambda: "spread", offload_plan=lambda *a: ["n1"])
    c = bare(fleet)
    assert c._offload_plan("r", 1) is None
    c.fleet_privileged = "priv:shell"
    assert c._offload_plan("r", 3) is None


def test_netsquad_plan_errors():
    assert bare(Boom())._netsquad_plan("r", 3) is None
    fleet = SimpleNamespace(enabled=True, squad_dispatch=lambda d: "network",
                            netsquad_plan=lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert bare(fleet)._netsquad_plan("r", 3) == (3, {})
    fleet.netsquad_plan = lambda *a: {0: "n1", 7: "n2", -1: "n3"}
    assert bare(fleet)._netsquad_plan("r", 3) == (3, {0: "n1"})
    assert bare(fleet)._netsquad_plan("r", 0) is None


def test_netsquad_helpers():
    c = bare()
    c.netsquad_path = ["a", 1]
    c.netsquad_budget = True
    assert c._netsquad_path() == ["a", "1"] and c._netsquad_budget() is None
    c.netsquad_budget = 3
    assert c._netsquad_budget() == 3


def _local():
    async def f():
        return "ran locally"
    return f


def test_offloaded_and_netsquad_fall_back_locally():
    fleet = SimpleNamespace(offload_worker=lambda *a: (_ for _ in ()).throw(ConnectionError()),
                            netsquad_worker=lambda *a: (_ for _ in ()).throw(TimeoutError()))

    async def go():
        c = bare(fleet)
        gate = asyncio.Semaphore(1)
        s = {"role": "R", "focus": "f"}
        placed = {}
        a = await c._offloaded("r", "n1", s, "t", gate, _local())
        b = await c._netsquad("r", 0, "n1", s, "t", 1, gate, _local(), placed)
        return a, b, placed
    a, b, placed = run(go())
    assert a == b == "ran locally" and placed == {}


def test_offloaded_cancel_propagates():
    async def cancel(*a):
        raise asyncio.CancelledError()
    fleet = SimpleNamespace(offload_worker=cancel, netsquad_worker=cancel)

    async def go():
        c = bare(fleet)
        with pytest.raises(asyncio.CancelledError):
            await c._offloaded("r", "n", {"role": "R", "focus": "f"}, "t", asyncio.Semaphore(1), _local())
        with pytest.raises(asyncio.CancelledError):
            await c._netsquad("r", 0, "n", {"role": "R", "focus": "f"}, "t", 1, asyncio.Semaphore(1), _local(), {})
    run(go())


def test_gather_agents_crash_becomes_note_and_cancel_propagates():
    seen = []
    c = bare()
    c.codex = SimpleNamespace(emit=lambda *a, **k: seen.append(k.get("message", "")))

    async def ok():
        return "fine"

    async def bad():
        raise ValueError("boom")

    out = run(c._gather_agents("r", [ok(), bad()]))
    assert out[0] == "fine" and out[1].startswith("[agent 2 failed: ValueError")
    assert any("crashed" in m for m in seen)

    async def cancelled():
        raise asyncio.CancelledError()

    async def go():
        return await c._gather_agents("r", [ok(), cancelled()])
    with pytest.raises(asyncio.CancelledError):
        run(go())
