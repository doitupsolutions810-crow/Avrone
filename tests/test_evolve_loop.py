"""Bounded self-evolution loop. Offline: real git clones of a tiny temp repo, but the test gate is a
FAKE command (grep/true/false) -- never pytest-in-pytest -- and time is a fake clock."""
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from avrone import evolve
from avrone.sovereign import CommandResult

GOOD = [{"path": "pkg/__init__.py", "content": "VALUE = 1\nEXTRA = 2\n"}]
BAD = [{"path": "pkg/__init__.py", "content": "VALUE = 2\n"}]
# Fake gate: "the suite passes" iff VALUE is still 1. No pytest is spawned.
GATE = "grep -q '^VALUE = 1$' pkg/__init__.py"


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


class FakeRunner:
    """Runs shell for git plumbing; records every command + timeout; advances the fake clock by
    `step` seconds whenever the gate (test) command runs."""
    def __init__(self, clock=None, step=0.0):
        self.cmds, self.clock, self.step = [], clock, step

    async def run(self, cmd, timeout=None):
        self.cmds.append((cmd, timeout))
        if self.clock is not None and "PYTHONPATH=" in cmd:
            self.clock.t += self.step
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
        return CommandResult(cmd, p.returncode, p.stdout, p.stderr)


def mkctx(tmp, runner, events=None):
    emit = (lambda run_id, kind, agent, **d: events.append((kind, d))) if events is not None \
        else (lambda *a, **k: None)
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp / "d")), runner=runner,
                           codex=SimpleNamespace(emit=emit))
    return SimpleNamespace(core=core, run_id="r1", name="Evolver-1", depth=0)


def mkrepo(tmp):
    r = tmp / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("VALUE = 1\n")
    for c in ("git init -q -b main", "git add -A", "git -c user.email=a@a -c user.name=a commit -qm base"):
        subprocess.run(["bash", "-c", f"cd {r} && {c}"], check=True)
    return r


def git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a], capture_output=True, text=True).stdout.strip()


def snapshot(r):
    return (git(r, "rev-parse", "HEAD"), git(r, "rev-parse", "--abbrev-ref", "HEAD"),
            git(r, "status", "--porcelain"), git(r, "branch", "--list"), (r / "pkg" / "__init__.py").read_text())


@pytest.fixture
def env(tmp_path, monkeypatch):
    r = mkrepo(tmp_path)
    monkeypatch.setenv("AVRONE_REPO_DIR", str(r))
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    clock = Clock()
    monkeypatch.setattr(evolve, "_now", clock)
    return SimpleNamespace(tmp=tmp_path, repo=r, clock=clock)


def run(c): return asyncio.run(c)


def test_stops_on_first_success(env):
    ctx = mkctx(env.tmp, FakeRunner())
    before = snapshot(env.repo)
    res = run(evolve.evolve_loop(ctx, "g", candidates=[BAD, BAD, GOOD, GOOD, GOOD], max_iters=5,
                                 time_budget_s=100, test_cmd=GATE))
    assert res["stopped"] == "accepted"
    assert [a["passed"] for a in res["attempts"]] == [False, False, True]   # 4th/5th never tried
    assert res["accepted"]["pid"] == res["attempts"][2]["pid"]
    assert res["accepted"]["branch"] == "evolve/" + res["attempts"][2]["pid"]
    assert snapshot(env.repo) == before                                      # main/live tree untouched
    # Accepted change committed on its branch inside its own clone under data_dir.
    wt = res["attempts"][2]["worktree"]
    assert wt.startswith(str(env.tmp / "d" / "evolve"))
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == res["accepted"]["branch"]
    assert "EXTRA" in git(wt, "show", "HEAD:pkg/__init__.py")


def test_failing_attempts_return_test_output_and_distinct_worktrees(env):
    ctx = mkctx(env.tmp, FakeRunner())
    gate = GATE + " || { echo 'FAILED test_v - assert 2 == 1'; exit 1; }"
    res = run(evolve.evolve_loop(ctx, "g", candidates=[BAD, BAD], max_iters=5, test_cmd=gate))
    assert res["stopped"] == "exhausted" and res["accepted"] is None
    assert all("FAILED test_v" in a["test_tail"] for a in res["attempts"])
    wts = {a["worktree"] for a in res["attempts"]}
    assert len(wts) == 2 and all(w.startswith(str(env.tmp / "d" / "evolve")) for w in wts)


def test_stops_on_iteration_cap(env):
    ctx = mkctx(env.tmp, FakeRunner())
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=lambda i, prev: BAD, max_iters=2, test_cmd=GATE))
    assert res["stopped"] == "max_iters" and len(res["attempts"]) == 2


def test_max_iters_hard_capped(env):
    ctx = mkctx(env.tmp, FakeRunner())
    calls = []

    def fn(i, prev):
        calls.append(i)
        return BAD
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=fn, max_iters=10_000, test_cmd="false"))
    assert res["max_iters"] == evolve.MAX_ITERS_CAP == 10
    assert len(res["attempts"]) == 10 and calls == list(range(10))


def test_time_budget_hard_capped_and_bounds_test_timeout(env):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    res = run(evolve.evolve_loop(ctx, "g", candidates=[GOOD], time_budget_s=10 ** 9, test_cmd=GATE))
    assert res["time_budget_s"] == evolve.TIME_BUDGET_CAP_S == 1800
    # the gate's timeout never exceeds the remaining budget
    env.clock.t = 0
    runner2 = FakeRunner()
    res = run(evolve.evolve_loop(mkctx(env.tmp, runner2), "g", candidates=[GOOD], time_budget_s=30,
                                 test_cmd=GATE))
    gate_timeouts = [t for c, t in runner2.cmds if "PYTHONPATH=" in c]
    assert gate_timeouts and max(gate_timeouts) <= 30


def test_stops_on_time_budget(env):
    # every gate run "takes" 40s of fake time; budget 100s -> at most 3 iterations, then stop
    ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=40))
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=lambda i, p: BAD, max_iters=10, time_budget_s=100,
                                 test_cmd=GATE))
    assert res["stopped"] == "time_budget"
    assert len(res["attempts"]) == 3


def test_budget_already_spent_runs_nothing(env):
    ctx = mkctx(env.tmp, FakeRunner())
    calls = []

    def fn(i, prev):
        calls.append(i)
        env.clock.t += 5  # generating the candidate eats the whole 1s budget
        return BAD
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=fn, max_iters=5, time_budget_s=1, test_cmd=GATE))
    assert res["stopped"] == "time_budget" and len(res["attempts"]) == 1


def test_edits_fn_sees_prior_failures_and_can_be_async(env):
    ctx = mkctx(env.tmp, FakeRunner())
    seen = []

    async def fn(i, prev):
        seen.append([a["passed"] for a in prev])
        return BAD if i == 0 else GOOD
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=fn, max_iters=4, test_cmd=GATE))
    assert res["stopped"] == "accepted" and seen == [[], [False]]


def test_deny_blocks_loop_and_tool(env, monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    res = run(evolve.evolve_loop(ctx, "g", candidates=[GOOD], test_cmd=GATE))
    assert res["stopped"] == "denied" and res["attempts"] == [] and "disabled" in res["error"]
    assert "disabled" in run(evolve.t_evolve_loop(ctx, {"goal": "g", "candidates": [GOOD]}))
    assert runner.cmds == []                                    # nothing ran, no clone made
    assert not (env.tmp / "d" / "evolve").exists() or not any((env.tmp / "d" / "evolve").iterdir())


def test_path_escape_rejected_in_loop(env):
    ctx = mkctx(env.tmp, FakeRunner())
    res = run(evolve.evolve_loop(ctx, "g", candidates=[[{"path": "../evil.py", "content": "x"}], GOOD],
                                 test_cmd=GATE))
    assert "escapes repo" in res["attempts"][0]["error"] and not res["attempts"][0]["passed"]
    assert not (env.tmp / "evil.py").exists()
    assert res["stopped"] == "accepted" and res["attempts"][1]["passed"]


def test_tool_wrapper_reports_each_attempt(env):
    ctx = mkctx(env.tmp, FakeRunner())
    out = run(evolve.t_evolve_loop(ctx, {"goal": "g", "candidates": [BAD, GOOD], "test_cmd": GATE,
                                         "max_iters": 50, "time_budget_s": 99999}))
    assert "stopped=accepted" in out and "REJECTED" in out and "ACCEPTED on branch evolve/" in out
    assert "10 iters" in out and "1800s" in out
    assert run(evolve.t_evolve_loop(ctx, {"goal": "g"})).startswith("ERROR")


def test_meta_tags_loop_iterations(env):
    ctx = mkctx(env.tmp, FakeRunner())
    res = run(evolve.evolve_loop(ctx, "g", candidates=[BAD, GOOD], test_cmd=GATE))
    for a in res["attempts"]:
        m = json.loads((env.tmp / "d" / "evolve" / f"{a['pid']}.json").read_text())
        assert m["loop_id"] == res["loop_id"] and m["iteration"] == a["iteration"]
    assert [x["pid"] for x in evolve.list_accepted(ctx.core.settings)] == [res["accepted"]["pid"]]


# ---- observability + duration controls --------------------------------------------------------

def test_per_iteration_timing_in_meta_codex_and_loop_json(env):
    events = []
    ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=7), events)
    res = run(evolve.evolve_loop(ctx, "g", candidates=[BAD, GOOD], time_budget_s=100, test_cmd=GATE))
    assert [a["duration_s"] for a in res["attempts"]] == [7, 7] and res["duration_s"] == 14
    for a in res["attempts"]:
        m = json.loads((env.tmp / "d" / "evolve" / f"{a['pid']}.json").read_text())
        assert m["duration_s"] == 7
    it_events = [d for k, d in events if k == "evolve.iteration"]
    assert [(e["iteration"], e["outcome"], e["duration_s"]) for e in it_events] == \
        [(0, "rejected", 7), (1, "accepted", 7)]
    assert all(e["loop_id"] == res["loop_id"] for e in it_events)
    (loop_ev,) = [d for k, d in events if k == "evolve.loop"]
    assert loop_ev["stopped"] == "accepted" and loop_ev["duration_s"] == 14
    lp = json.loads((env.tmp / "d" / "evolve" / "loops" / f"{res['loop_id']}.json").read_text())
    assert lp["status"] == "done" and lp["stopped"] == "accepted" and lp["duration_s"] == 14
    assert [(i["outcome"], i["duration_s"]) for i in lp["iterations"]] == [("rejected", 7), ("accepted", 7)]
    assert lp["accepted"]["pid"] == res["accepted"]["pid"]


def test_loop_json_written_while_running(env):
    ctx = mkctx(env.tmp, FakeRunner())
    snaps = []

    def fn(i, prev):
        (lp,) = evolve.list_loops(ctx.core.settings)
        snaps.append((lp["status"], len(lp["iterations"])))
        return BAD
    run(evolve.evolve_loop(ctx, "g", edits_fn=fn, max_iters=2, test_cmd=GATE))
    assert snaps == [("running", 0), ("running", 1)]


def test_evolve_status_and_list(env):
    ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=3))
    assert "no evolve loops yet" in run(evolve.t_evolve_status(ctx, {}))
    res = run(evolve.evolve_loop(ctx, "fix value", candidates=[BAD, BAD, GOOD], max_iters=5, test_cmd=GATE))
    st = run(evolve.t_evolve_status(ctx, {}))
    assert res["loop_id"] in st and "iters=3/5" in st and "accepted=1" in st and "rejected=2" in st
    assert st.count("REJECTED") == 2 and st.count("ACCEPTED") == 1 and "3.00s" in st
    assert f"on branch evolve/{res['accepted']['pid']}" in st
    assert res["loop_id"] in run(evolve.t_evolve_status(ctx, {"loop_id": res["loop_id"]}))
    assert run(evolve.t_evolve_status(ctx, {"loop_id": "nope"})).startswith("ERROR")
    ls = run(evolve.t_evolve_list(ctx, {}))
    assert "loops:" in ls and f"loop={res['loop_id']}#2" in ls and "3.0s" in ls
    assert ls.count("[FAIL]") == 2 and ls.count("[PASS]") == 1
    assert "done/accepted" in ls


def test_env_max_iters_default_and_ceiling(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "2")
    assert evolve.loop_limits() == (2, evolve.DEFAULT_TIME_BUDGET_S)
    assert evolve.loop_limits(max_iters=1)[0] == 1           # agent may ask for fewer
    assert evolve.loop_limits(max_iters=9)[0] == 2           # never more than the node allows
    ctx = mkctx(env.tmp, FakeRunner())
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=lambda i, p: BAD, max_iters=9, test_cmd=GATE))
    assert res["stopped"] == "max_iters" and len(res["attempts"]) == 2
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "500")      # env can't lift the hard cap
    assert evolve.loop_limits()[0] == evolve.MAX_ITERS_CAP
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "junk")     # garbage -> built-in default
    assert evolve.loop_limits()[0] == evolve.DEFAULT_MAX_ITERS


def test_env_time_budget_default_and_ceiling(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "50")
    assert evolve.loop_limits()[1] == 50
    assert evolve.loop_limits(time_budget_s=20)[1] == 20
    assert evolve.loop_limits(time_budget_s=1000)[1] == 50
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "99999")
    assert evolve.loop_limits()[1] == evolve.TIME_BUDGET_CAP_S
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "-5")
    assert evolve.loop_limits()[1] == evolve.DEFAULT_TIME_BUDGET_S
    # budget from env actually stops the loop: 30s/iteration against a 50s env budget -> 2 iters
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "50")
    ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=30))
    res = run(evolve.evolve_loop(ctx, "g", edits_fn=lambda i, p: BAD, max_iters=10, test_cmd=GATE))
    assert res["stopped"] == "time_budget" and len(res["attempts"]) == 2
    lp = evolve.list_loops(ctx.core.settings)[-1]
    assert lp["time_budget_s"] == 50 and lp["stopped"] == "time_budget" and lp["duration_s"] == 60


def test_single_propose_records_duration(env):
    ctx = mkctx(env.tmp, FakeRunner(clock=env.clock, step=4))
    out = run(evolve.t_evolve_propose(ctx, {"goal": "g", "edits": GOOD, "test_cmd": GATE}))
    assert "ACCEPTED" in out
    m = json.loads((env.tmp / "d" / "evolve" / f"{out.split()[1].rstrip(':')}.json").read_text())
    assert m["duration_s"] == 4 and "loop_id" not in m
