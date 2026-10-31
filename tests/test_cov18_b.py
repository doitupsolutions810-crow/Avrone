"""Round 18 coverage: an evolve loop whose time budget is already spent before its first iteration
(clock jump, a resumed loop out of time) stops as time_budget with no attempt run."""
import asyncio

from avrone import evolve
from tests.test_evolve_loop import GATE, GOOD, FakeRunner, mkctx, mkrepo


class JumpClock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls == 1 else 10.0 ** 6


def test_budget_spent_before_first_iteration(tmp_path, monkeypatch):
    repo = mkrepo(tmp_path)
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo))
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    monkeypatch.setattr(evolve, "_now", JumpClock())
    runner = FakeRunner()
    res = asyncio.run(evolve.evolve_loop(mkctx(tmp_path, runner), "g", candidates=[GOOD], max_iters=3,
                                         time_budget_s=60, test_cmd=GATE))
    assert res["stopped"] == "time_budget" and res["attempts"] == [] and res["iterations_total"] == 0
    assert not any(GATE in c for c, _ in runner.cmds)  # the gate never ran
