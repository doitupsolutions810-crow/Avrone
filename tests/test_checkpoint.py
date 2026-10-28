"""Offline tests: checkpoint/resume for long squad and deep runs."""
import asyncio
import json

import pytest

from avrone import cli
from avrone.checkpoint import Checkpoint
from avrone.codex import Codex
from avrone.config import Settings
from avrone.llm import LLMError
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore

PLAN3 = json.dumps({"subtasks": [
    {"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}, {"role": "C", "focus": "gamma"}]})


def report(corrections="- fix one", bottom="better answer"):
    return ("## Verdict\nCorrected. Confidence: High\n\n"
            f"## Corrections\n{corrections}\n\n## Additions\n- x\n\n## Counterpoints\n- y\n\n"
            f"## Sources\n- https://example.com\n\n## Bottom line\n{bottom}")


class CountingLLM:
    """Scripted model that counts every call by kind; `boom` maps a focus/role to a failure."""

    def __init__(self, boom=None, deep_leads=None):
        self.boom = dict(boom or {})
        self.deep_leads = list(deep_leads or [report()])
        self.calls = {"plan": 0, "worker": 0, "lead": 0, "deeplead": 0, "reviewer": 0}
        self.workers_seen: list[str] = []

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            self.calls["plan"] += 1
            return PLAN3
        if "You are the DEEP LEAD" in system:
            self.calls["deeplead"] += 1
            r = self.deep_leads[min(self.calls["deeplead"], len(self.deep_leads)) - 1]
            if isinstance(r, BaseException):
                raise r
            return r
        if "You are the LEAD" in system:
            self.calls["lead"] += 1
            return "LEAD MERGED " + "x" * 400
        for key, what in self.boom.items():
            if f"Your focus: {key}" in system or f"the {key} reviewer" in system:
                if what == "hang":
                    await asyncio.sleep(3600)
                raise what
        if "DEEP ANALYSIS" in system:
            self.calls["reviewer"] += 1
            return '{"final": "review ok"}'
        self.calls["worker"] += 1
        self.workers_seen.append(system.split("Your focus: ", 1)[1].split("\n", 1)[0])
        return '{"final": "worker ok"}'


def _core(tmp_path, llm, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 2
    for k, v in over.items():
        setattr(s, k, v)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))


def _kinds(core, run_id):
    return [json.loads(l)["kind"] for l in (core.codex.log_dir / f"{run_id}.jsonl").read_text().splitlines()]


# ------------------------------------------------------------------ the file itself
def test_checkpoint_persists_atomically_and_scopes(tmp_path):
    p = tmp_path / "sub" / "ck.json"
    ck = Checkpoint(p)
    ck.put("a", 1)
    ck.scope("w.").put("b", {"x": [1, 2]})
    again = Checkpoint(p)
    assert again.get("a") == 1 and again.scope("w.").get("b") == {"x": [1, 2]}
    assert sorted(again.keys()) == ["a", "w.b"] and again.scope("w.").keys() == ["b"]
    assert not list(p.parent.glob("*.tmp"))  # temp files never left behind
    again.scope("w.").clear()
    assert Checkpoint(p).keys() == ["a"]
    assert again.summary() == {"path": str(p), "steps": 1, "done": False, "write_errors": 0}


def test_coerce():
    assert Checkpoint.coerce(None) is None and Checkpoint.coerce("") is None
    ck = Checkpoint.coerce("/tmp/avrone-ck-test-unused.json")
    assert isinstance(ck, Checkpoint) and Checkpoint.coerce(ck) is ck


def test_corrupt_or_foreign_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "ck.json"
    p.write_text("{not json")
    assert Checkpoint(p).keys() == []
    p.write_text(json.dumps({"version": 99, "steps": {"x": 1}}))
    assert Checkpoint(p).keys() == []


def test_unwritable_checkpoint_never_raises(tmp_path):
    (tmp_path / "file").write_text("I am a file, not a dir")
    ck = Checkpoint(tmp_path / "file" / "ck.json")
    ck.put("a", 1)  # cannot be written: kept in memory, counted
    ck.put("b", object())  # still unwritable (objects are saved via default=str)
    assert ck.get("a") == 1 and ck.summary()["write_errors"] == 2


def test_temp_file_removed_when_dump_fails(tmp_path, monkeypatch):
    import avrone.checkpoint as mod
    ck = Checkpoint(tmp_path / "ck.json")

    def bad_dump(*a, **k):
        raise TypeError("nope")
    monkeypatch.setattr(mod.json, "dump", bad_dump)
    ck.put("a", 1)
    assert ck.summary()["write_errors"] == 1 and not list(tmp_path.glob("*.tmp"))


def test_resume_point_clears_a_different_run(tmp_path):
    ck = Checkpoint(tmp_path / "ck.json")
    assert ck.resume_point("t", "squad", n=3) is None
    ck.begin("t", "squad", "r1", n=3)
    ck.put("worker.0", "done")
    assert ck.resume_point("t", "squad", n=3)["run_id"] == "r1"
    assert ck.resume_point("other task", "squad", n=3) is None
    assert ck.keys() == []  # stale results can never leak into the new run


# ------------------------------------------------------------------ squad resume
def test_completed_squad_with_a_failed_worker_is_returned_as_is(tmp_path):
    p = tmp_path / "ck.json"
    llm = CountingLLM(boom={"beta": LLMError("backend down")})
    first = asyncio.run(_core(tmp_path, llm).squad("task", n=3, checkpoint=p))
    assert "failed" in first["agents"][1]["result"]
    assert Checkpoint(p).get("result")["run_id"] == first["run_id"]

    # the stored run is complete (the lead merged), so the same call returns it untouched
    llm2 = CountingLLM()
    again = asyncio.run(_core(tmp_path, llm2).squad("task", n=3, checkpoint=p))
    assert again == first and sum(llm2.calls.values()) == 0


def test_interrupted_squad_resumes_without_replanning(tmp_path):
    p = tmp_path / "ck.json"
    llm = CountingLLM(boom={"beta": "hang"})
    core = _core(tmp_path, llm)

    async def interrupted():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(core.squad("task", n=3, run_id="long", checkpoint=p), 0.5)
    asyncio.run(interrupted())
    ck = Checkpoint(p)
    assert ck.get("subtasks") and ck.get("worker.0") == "worker ok" and ck.get("worker.2") == "worker ok"
    assert ck.get("worker.1") is None and ck.get("result") is None

    llm2 = CountingLLM()
    core2 = _core(tmp_path, llm2)
    out = asyncio.run(core2.squad("task", n=3, checkpoint=p))  # run_id comes from the checkpoint
    assert out["run_id"] == "long"
    assert [a["result"] for a in out["agents"]] == ["worker ok"] * 3
    assert llm2.calls["plan"] == 0 and llm2.calls["worker"] == 1 and llm2.workers_seen == ["beta"]
    assert llm2.calls["lead"] == 1
    assert "checkpoint.resume" in _kinds(core2, "long")
    assert Checkpoint(p).summary()["done"] is True


def test_failed_lead_is_retried_on_resume(tmp_path):
    p = tmp_path / "ck.json"

    class LeadDown(CountingLLM):
        async def chat(self, messages, **kw):
            if "You are the LEAD" in messages[0]["content"]:
                raise RuntimeError("lead offline")
            return await super().chat(messages, **kw)
    out = asyncio.run(_core(tmp_path, LeadDown()).squad("task", n=3, checkpoint=p))
    assert out["answer"].startswith("Lead failed")
    assert Checkpoint(p).get("result") is None
    llm2 = CountingLLM()
    out = asyncio.run(_core(tmp_path, llm2).squad("task", n=3, checkpoint=p))
    assert out["answer"].startswith("LEAD MERGED") and llm2.calls["worker"] == 0


def test_new_task_on_same_file_starts_fresh(tmp_path):
    p = tmp_path / "ck.json"
    asyncio.run(_core(tmp_path, CountingLLM()).squad("task one", n=3, checkpoint=p))
    llm = CountingLLM()
    asyncio.run(_core(tmp_path, llm).squad("task two", n=3, checkpoint=p))
    assert llm.calls["plan"] == 1 and llm.calls["worker"] == 3
    assert Checkpoint(p).get("meta")["task"] == "task two"


def test_bad_checkpoint_argument_does_not_sink_the_run(tmp_path, monkeypatch):
    import avrone.trinity as tr
    monkeypatch.setattr(tr.Checkpoint, "coerce", classmethod(lambda cls, o: 1 / 0))
    out = asyncio.run(_core(tmp_path, CountingLLM()).squad("task", n=3, checkpoint=tmp_path / "x"))
    assert out["answer"].startswith("LEAD MERGED")


def test_planner_error_is_not_checkpointed(tmp_path):
    p = tmp_path / "ck.json"

    class PlanDown(CountingLLM):
        async def chat(self, messages, **kw):
            if "PLANNER" in messages[0]["content"]:
                raise LLMError("no model")
            return await super().chat(messages, **kw)
    out = asyncio.run(_core(tmp_path, PlanDown()).squad("task", n=3, checkpoint=p))
    assert out["answer"].startswith("Model backend error")
    ck = Checkpoint(p)
    assert ck.get("subtasks") is None and ck.get("result") is None


# ------------------------------------------------------------------ deep resume
def test_deep_resumes_after_lead_failure_without_redoing_rounds(tmp_path):
    p = tmp_path / "ck.json"
    llm = CountingLLM(deep_leads=[report("- fix one"), LLMError("lead died")])
    out = asyncio.run(_core(tmp_path, llm).deep("q", first_answer="a", n=2, rounds=3, checkpoint=p))
    assert out["stop_reason"] == "lead_failed" and llm.calls["reviewer"] == 4
    ck = Checkpoint(p)
    assert len(ck.get("rounds")["history"]) == 1 and ck.get("result") is None
    assert ck.get("round2.reviewer.0") == "review ok"

    # round 1 is done and round 2's reviews are cached: only round 2's lead (and round 3) run
    llm2 = CountingLLM(deep_leads=[report("- fix two", "best answer"), report("None")])
    core2 = _core(tmp_path, llm2)
    out2 = asyncio.run(core2.deep("q", first_answer="a", n=2, rounds=3, checkpoint=p))
    assert out2["run_id"] == out["run_id"]
    assert out2["rounds_run"] == 3 and out2["converged"] and out2["stop_reason"] == "converged"
    assert llm2.calls["reviewer"] == 2 and llm2.calls["deeplead"] == 2
    assert [h["round"] for h in out2["rounds"]] == [1, 2, 3]
    assert "checkpoint.resume" in _kinds(core2, out["run_id"])

    # finished: returned from the checkpoint with no model calls
    llm3 = CountingLLM()
    assert asyncio.run(_core(tmp_path, llm3).deep("q", first_answer="a", n=2, rounds=3,
                                                  checkpoint=p)) == out2
    assert sum(llm3.calls.values()) == 0


def test_deep_converged_state_is_final(tmp_path):
    p = tmp_path / "ck.json"
    llm = CountingLLM(deep_leads=[report("None")])
    out = asyncio.run(_core(tmp_path, llm).deep("q", first_answer="a", n=1, rounds=2, checkpoint=p))
    assert out["converged"]
    ck = Checkpoint(p)
    assert ck.get("rounds")["stop_reason"] == "converged"
    # even with the final result lost, the saved rounds say it is over: no new round runs
    steps = json.loads(p.read_text())
    del steps["steps"]["result"]
    p.write_text(json.dumps(steps))
    llm2 = CountingLLM()
    out2 = asyncio.run(_core(tmp_path, llm2).deep("q", first_answer="a", n=1, rounds=2, checkpoint=p))
    assert out2["rounds_run"] == 1 and out2["stop_reason"] == "converged"
    assert llm2.calls["reviewer"] == 0 and llm2.calls["deeplead"] == 0


def test_deep_cap_and_no_reviews_states(tmp_path):
    p = tmp_path / "ck.json"
    out = asyncio.run(_core(tmp_path, CountingLLM()).deep("q", first_answer="a", n=0, checkpoint=p))
    assert out["stop_reason"] == "no_reviews"
    assert Checkpoint(p).get("rounds")["stop_reason"] == "no_reviews"
    p2 = tmp_path / "ck2.json"
    out = asyncio.run(_core(tmp_path, CountingLLM()).deep("q", first_answer="a", n=1, rounds=1, checkpoint=p2))
    assert out["stop_reason"] == "cap"
    st = json.loads(p2.read_text())
    del st["steps"]["result"]
    p2.write_text(json.dumps(st))
    llm = CountingLLM()
    again = asyncio.run(_core(tmp_path, llm).deep("q", first_answer="a", n=1, rounds=1, checkpoint=p2))
    assert again["stop_reason"] == "cap" and again["rounds_run"] == 1 and sum(llm.calls.values()) == 0


def test_deep_first_wave_lives_in_its_own_scope(tmp_path):
    p = tmp_path / "ck.json"
    llm = CountingLLM(deep_leads=[report("None")])
    out = asyncio.run(_core(tmp_path, llm).deep("q", n=1, rounds=1, checkpoint=p))
    assert out["first_run_id"].endswith("-wave1")
    ck = Checkpoint(p)
    assert ck.scope("wave1.").get("result")["answer"].startswith("LEAD MERGED")
    assert ck.scope("wave1.").get("meta")["mode"] == "squad"


def test_deep_with_a_different_first_answer_starts_fresh(tmp_path):
    p = tmp_path / "ck.json"
    asyncio.run(_core(tmp_path, CountingLLM()).deep("q", first_answer="a", n=1, rounds=1, checkpoint=p))
    llm = CountingLLM()
    asyncio.run(_core(tmp_path, llm).deep("q", first_answer="b", n=1, rounds=1, checkpoint=p))
    assert llm.calls["reviewer"] == 1


# ------------------------------------------------------------------ CLI
def test_cli_checkpoint_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(tmp_path / "np"))
    monkeypatch.setenv("AVRONE_MCP_CONFIG", str(tmp_path / "none.json"))
    p = tmp_path / "squad.json"
    assert cli.main(["squad", "hello", "--quiet", "--no-extras", "-n", "2", "--checkpoint", str(p)]) == 0
    first = capsys.readouterr().out
    assert Checkpoint(p).get("result")["answer"].strip() == first.strip()
    assert cli.main(["squad", "hello", "--quiet", "--no-extras", "-n", "2", "--checkpoint", str(p)]) == 0
    assert capsys.readouterr().out == first
    d = tmp_path / "deep.json"
    assert cli.main(["deep", "q?", "--first-answer", "a", "--quiet", "--no-extras", "-n", "1",
                     "--checkpoint", str(d)]) == 0
    assert Checkpoint(d).get("result")["mode"] == "deep"
    args = cli.build_parser().parse_args(["squad", "x"])
    assert args.checkpoint is None
