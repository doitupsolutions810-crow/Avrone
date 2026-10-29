"""Offline edge tests for avrone.evolve: refusal paths, clamping, clone fallback, flaky retry,
background crash handling, and the status/list/diff/reset tools on missing or corrupt state.
Git plumbing runs for real on a tiny temp repo; the test gate is always a fake command."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import evolve
from avrone.sovereign import CommandResult
from tests.test_evolve_loop import GATE, GOOD, Clock, FakeRunner, mkctx, mkrepo


def run(c):
    return asyncio.run(c)


@pytest.fixture
def env(tmp_path, monkeypatch):
    r = mkrepo(tmp_path)
    monkeypatch.setenv("AVRONE_REPO_DIR", str(r))
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_MAX_BG", raising=False)
    monkeypatch.setattr(evolve, "_now", Clock())
    evolve._BG.clear()
    yield SimpleNamespace(tmp=tmp_path, repo=r)
    evolve._BG.clear()


# ------------------------------------------------------------ small helpers
def test_repo_root_defaults_to_package_parent(monkeypatch):
    monkeypatch.delenv("AVRONE_REPO_DIR", raising=False)
    assert evolve._repo_root(None) == Path(evolve.__file__).resolve().parent.parent


def test_emit_swallows_codex_errors(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("codex down")
    ctx = SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=boom)), run_id="r", name="n")
    evolve._emit(ctx, "evolve.x", a=1)  # must not raise


@pytest.mark.parametrize("v,expected", [
    (None, 5.0), ("junk", 5.0), ([1], 5.0), (float("nan"), 5.0),
    (0, 1.0), (-3, 1.0), (999, 10.0), ("7", 7.0),
])
def test_clamp_junk_nan_and_bounds(v, expected):
    assert evolve._clamp(v, default=5, cap=10) == expected


# ------------------------------------------------------------ worktree creation
class ScriptedRunner(FakeRunner):
    """Real bash, except commands matching a key in `fake` return that exit code without running."""
    def __init__(self, fake):
        super().__init__()
        self.fake = fake

    async def run(self, cmd, timeout=None):
        for needle, codes in self.fake.items():
            if needle in cmd and codes:
                self.cmds.append((cmd, timeout))
                code = codes.pop(0)
                return CommandResult(cmd, code, f"faked {needle} rc={code}\n", "")
        return await super().run(cmd, timeout)


def test_ensure_worktree_reuses_existing_clone(env):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    wt1, b1 = run(evolve._ensure_worktree(ctx, "p1"))
    n = len(runner.cmds)
    wt2, b2 = run(evolve._ensure_worktree(ctx, "p1"))
    assert (wt1, b1) == (wt2, b2) == (wt1, "evolve/p1")
    assert len(runner.cmds) == n  # second call ran no git at all


def test_ensure_worktree_falls_back_to_copy_when_clone_fails(env):
    (env.repo / "data").mkdir()
    (env.repo / "data" / "secret.db").write_text("live state")
    runner = ScriptedRunner({"git clone": [128]})
    ctx = mkctx(env.tmp, runner)
    wt, branch = run(evolve._ensure_worktree(ctx, "p2"))
    assert (wt / "pkg" / "__init__.py").read_text() == "VALUE = 1\n"
    assert not (wt / "data").exists()  # runtime data never copied into the proposal
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "evolve/p2"


# ------------------------------------------------------------ flaky retry
def test_pytest_gate_flake_is_retried_with_lf(env):
    runner = ScriptedRunner({"--lf": [0], "PYTHONPATH=": [1]})
    ctx = mkctx(env.tmp, runner)
    r = run(evolve._propose_once(ctx, "g", GOOD, test_cmd="python -m pytest -q"))
    assert r["passed"] is True and r["flaky_retry"] is True
    assert "rc=0" in r["test_tail"]  # the tail reported is from the passing re-run
    meta = json.loads(evolve._meta_path(ctx, r["pid"]).read_text())
    assert meta["flaky_retry"] is True


def test_non_pytest_gate_failure_is_not_retried(env):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    r = run(evolve._propose_once(ctx, "g", GOOD, test_cmd="false"))
    assert r["passed"] is False and r["flaky_retry"] is False
    assert not any("--lf" in c for c, _ in runner.cmds)


# ------------------------------------------------------------ refusal / invalid args
def test_propose_tool_refuses_when_denied_and_on_bad_args(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())
    assert run(evolve.t_evolve_propose(ctx, {"goal": "g"})).startswith("ERROR: give")
    assert run(evolve.t_evolve_propose(ctx, {"goal": " ", "edits": GOOD})).startswith("ERROR: give")
    assert run(evolve.t_evolve_propose(ctx, {"goal": "g", "edits": []})).startswith("ERROR: give")
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "DENY")
    assert run(evolve.t_evolve_propose(ctx, {"goal": "g", "edits": GOOD})) == evolve.DENIED_MSG
    assert run(evolve.t_evolve_loop(ctx, {"goal": "g", "candidates": [GOOD]})) == evolve.DENIED_MSG
    assert run(evolve.t_evolve_start(ctx, {"goal": "g", "candidates": [GOOD]})) == evolve.DENIED_MSG
    assert run(evolve.t_evolve_skill_propose(ctx, {"name": "x", "content": "y"})) == evolve.DENIED_MSG
    assert run(evolve.evolve_start(ctx, "g", candidates=[GOOD])) == {"error": evolve.DENIED_MSG, "loop_id": None}
    assert run(evolve.evolve_loop(ctx, "g", candidates=[GOOD]))["stopped"] == "denied"


def test_loop_and_start_reject_missing_goal_or_candidates(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "", candidates=[GOOD]))
    assert r["stopped"] == "invalid" and r["attempts"] == []
    assert run(evolve.evolve_start(ctx, "g"))["loop_id"] is None
    assert run(evolve.evolve_start(ctx, "  ", candidates=[GOOD]))["error"].startswith("ERROR: give")
    for bad in (None, [], [[]], [GOOD, "nope"]):
        assert run(evolve.t_evolve_start(ctx, {"goal": "g", "candidates": bad})).startswith("ERROR: give")
        assert run(evolve.t_evolve_loop(ctx, {"goal": "g", "candidates": bad})).startswith("ERROR: give")


def test_skill_tool_requires_name_and_content(env):
    ctx = mkctx(env.tmp, FakeRunner())
    assert run(evolve.t_evolve_skill_propose(ctx, {"content": "x"})).startswith("ERROR: give")
    assert run(evolve.t_evolve_skill_propose(ctx, {"name": "ok-name"})).startswith("ERROR: give")
    out = run(evolve.t_evolve_skill_propose(ctx, {"name": "Bad/Name", "content": "x"}))
    assert out.startswith("ERROR: bad skill name")


def test_start_refuses_past_background_cap(env, monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", "1")
    ctx = mkctx(env.tmp, FakeRunner())

    async def go():
        blocker = asyncio.get_running_loop().create_future()
        evolve._BG["busy"] = asyncio.ensure_future(blocker)
        try:
            return await evolve.evolve_start(ctx, "g", candidates=[GOOD])
        finally:
            evolve._BG["busy"].cancel()
    r = run(go())
    assert r["loop_id"] is None and "1 background evolve loop(s) already running" in r["error"]


# ------------------------------------------------------------ background crash handling
def test_background_loop_crash_is_recorded_not_left_running(env, monkeypatch):
    ctx = mkctx(env.tmp, FakeRunner())

    def explode(i, attempts):
        raise RuntimeError("generator blew up")

    async def go():
        r = await evolve.evolve_start(ctx, "g", edits_fn=explode)
        res = await evolve.evolve_wait(r["loop_id"], timeout=30)
        return r["loop_id"], res
    lid, res = run(go())
    assert res == {"loop_id": lid, "error": "generator blew up", "stopped": "error", "status": "error"}
    meta = json.loads(evolve._loop_path(ctx, lid).read_text())
    assert meta["status"] == "error" and meta["stopped"].startswith("error: generator blew up")
    assert lid not in evolve._BG  # finally-block dropped the strong ref


def test_evolve_wait_unknown_loop_is_none():
    assert run(evolve.evolve_wait("never-started")) is None


def test_finish_abnormally_tolerates_missing_or_corrupt_meta(env):
    ctx = mkctx(env.tmp, FakeRunner())
    evolve._cancel_path(ctx, "ghost").write_text("1")
    evolve._finish_abnormally(ctx, "ghost", "cancelled", "cancelled")
    m = json.loads(evolve._loop_path(ctx, "ghost").read_text())
    assert m == {"loop_id": "ghost", "iterations": [], "status": "cancelled", "stopped": "cancelled"}
    assert not evolve._cancel_path(ctx, "ghost").exists()
    evolve._loop_path(ctx, "corrupt").write_text("{not json")
    evolve._finish_abnormally(ctx, "corrupt", "error", "error: x")
    assert json.loads(evolve._loop_path(ctx, "corrupt").read_text())["status"] == "error"


# ------------------------------------------------------------ status / list / diff / reset tools
def test_status_tool_empty_unknown_and_background_running(env):
    ctx = mkctx(env.tmp, FakeRunner())
    assert run(evolve.t_evolve_status(ctx, {})).startswith("no evolve loops yet (limits: max_iters=")
    evolve._loop_path(ctx, "bg1").write_text(json.dumps(
        {"loop_id": "bg1", "goal": "g", "ts": 1, "max_iters": 3, "time_budget_s": 60, "status": "running",
         "stopped": None, "accepted": None, "iterations": [], "mode": "background", "owner_pid": None}))
    assert run(evolve.t_evolve_status(ctx, {"loop_id": "nope"})) == "ERROR: no loop nope. evolve_list to see them."
    out = run(evolve.t_evolve_status(ctx, {"loop_id": "bg1"}))
    assert "background loop" in out and "cancel requested" not in out
    assert run(evolve.t_evolve_cancel(ctx, {"loop_id": "bg1"})).startswith("cancel requested for loop bg1")
    assert "(cancel requested)" in run(evolve.t_evolve_status(ctx, {"loop_id": "bg1"}))
    assert run(evolve.t_evolve_cancel(ctx, {"loop_id": ""})).startswith("ERROR: no loop")


def test_cancel_tool_on_finished_loop(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "g", candidates=[GOOD], test_cmd=GATE))
    out = run(evolve.t_evolve_cancel(ctx, {"loop_id": r["loop_id"]}))
    assert out == f"loop {r['loop_id']} is done: nothing to cancel"


def test_list_tools_skip_corrupt_json(env):
    ctx = mkctx(env.tmp, FakeRunner())
    run(evolve.evolve_loop(ctx, "goal-ok", candidates=[GOOD], test_cmd=GATE))
    d = evolve._data(ctx)
    (d / "zz-broken.json").write_text("{nope")
    (d / "loops" / "zz-broken.json").write_text("{nope")
    listing = run(evolve.t_evolve_list(ctx, {}))
    assert "[PASS] goal-ok" in listing and "loops:" in listing
    assert len(evolve.list_loops(ctx.core.settings)) == 1
    acc = evolve.list_accepted(ctx.core.settings)
    assert len(acc) == 1 and acc[0]["goal"] == "goal-ok"


def test_diff_and_reset_unknown_pid(env):
    ctx = mkctx(env.tmp, FakeRunner())
    assert run(evolve.t_evolve_diff(ctx, {"pid": "nope"})).startswith("ERROR: no proposal nope")
    assert run(evolve.t_evolve_reset(ctx, {"pid": "nope"})) == "ERROR: no proposal nope"
    assert run(evolve.t_evolve_list(ctx, {})) == "no proposals yet"
