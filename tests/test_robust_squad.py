"""Robustness: one bad worker or a junk plan must not sink the whole squad."""
import asyncio
import json

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore


class FakeLLM:
    """Planner/lead replies are scripted; workers named in `boom` raise or hang."""

    def __init__(self, plan_reply: str, boom: dict[str, BaseException | str] | None = None):
        self.plan_reply = plan_reply
        self.boom = boom or {}
        self.lead_inputs: list[str] = []

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            return self.plan_reply
        if "You are the LEAD" in system:
            self.lead_inputs.append(messages[1]["content"])
            return "LEAD MERGED " + ("x" * 400)
        for focus, what in self.boom.items():
            if f"Your focus: {focus}" in system:
                if what == "hang":
                    await asyncio.sleep(3600)
                raise what  # type: ignore[misc]
        return '{"final": "worker ok"}'


def _core(tmp_path, llm, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 3
    for k, v in over.items():
        setattr(s, k, v)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))


PLAN3 = json.dumps({"subtasks": [
    {"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}, {"role": "C", "focus": "gamma"}]})


def test_one_worker_raising_non_llm_error_does_not_sink_squad(tmp_path):
    llm = FakeLLM(PLAN3, boom={"beta": RuntimeError("socket exploded")})
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("task", n=3, run_id="r1"))
    assert out["answer"].startswith("LEAD MERGED")
    results = [a["result"] for a in out["agents"]]
    assert results[0] == "worker ok" and results[2] == "worker ok"
    assert "failed" in results[1] and "socket exploded" in results[1]
    # the lead still got the two good reports
    assert llm.lead_inputs and llm.lead_inputs[0].count("worker ok") == 2
    # no agent is left stuck as "working" on the HUD
    assert all(a["status"] == "done" for a in core.active.values())
    kinds = [e["kind"] for e in core.codex.replay("r1")]
    assert kinds[-1] == "run.done" and kinds.count("agent.done") == 3


def test_hung_worker_times_out_and_rest_merge(tmp_path):
    llm = FakeLLM(PLAN3, boom={"gamma": "hang"})
    core = _core(tmp_path, llm, agent_timeout=0.5)
    out = asyncio.run(asyncio.wait_for(core.squad("task", n=3, run_id="r2"), 10))
    results = [a["result"] for a in out["agents"]]
    assert results[:2] == ["worker ok", "worker ok"]
    assert "timed out" in results[2]
    assert all(a["status"] == "done" for a in core.active.values())
    assert not core.message_agent(next(iter(core.active)), "hello")


def test_failed_worker_marked_done_and_inbox_cleared(tmp_path):
    llm = FakeLLM(PLAN3, boom={"alpha": ValueError("bad")})
    core = _core(tmp_path, llm)
    res = asyncio.run(core.run_agent("r3", "A", "task", focus="alpha"))
    assert res.startswith("[A-1 failed") and "ValueError" in res
    assert core.active["A-1"]["status"] == "done"
    assert "A-1" not in core.inbox


@pytest.mark.parametrize("plan", [
    '{"subtasks": [{"role": null, "focus": "x"}, {"role": 7, "focus": "y"}]}',  # non-str roles
    '{"subtasks": 5}',                                                           # not a list
    '{"subtasks": "abc"}',                                                       # a string
    '{"subtasks": [{"role": "R", "focus": {"nested": [1, 2]}}]}',                # non-str focus
    '[1, 2, 3]',                                                                 # top-level list
    'no json at all',
    '{"subtasks": []}',
])
def test_junk_plan_is_normalized(tmp_path, plan):
    core = _core(tmp_path, FakeLLM(plan))
    subs = asyncio.run(core.plan("r4", "task", 3))
    assert 1 <= len(subs) <= 3
    for s in subs:
        assert isinstance(s["role"], str) and s["role"]
        assert isinstance(s["focus"], str) and s["focus"]
    # and a full squad over that plan completes
    out = asyncio.run(core.squad("task", n=3, run_id="r5"))
    assert out["answer"].startswith("LEAD MERGED")


def test_plan_caps_oversized_subtask_list(tmp_path):
    plan = json.dumps({"subtasks": [{"role": f"R{i}", "focus": f"f{i}"} for i in range(50)]})
    core = _core(tmp_path, FakeLLM(plan))
    assert len(asyncio.run(core.plan("r6", "task", 4))) == 4


def test_stub_lane_exercises_bridge_edges_and_records_them(tmp_path):
    """Deterministic stand-in for a squad lane when no local model is installed.

    The worker is the real agent loop. Its scripted replies call bridge_post and
    bridge_read with an RTL mark and a combining-character flood, then the run
    writes what that lane produced under the artifacts directory.
    """
    import logging
    from datetime import datetime, timezone
    from avrone.bridge import register_bridge_tools
    from avrone.codex import Codex
    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    from avrone.tools import ToolRegistry
    from avrone.trinity import TrinityCore

    class LaneLLM:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, **kw):
            system = messages[0]["content"]
            self.calls.append(system[:40])
            if "You are the PLANNER" in system:
                return json.dumps({"subtasks": [{"role": "Edge", "focus": "blackboard edges"}]})
            if "You are the LEAD" in system:
                return "LEAD OK " + messages[-1]["content"][-80:]
            n = sum(1 for m in messages if m["role"] == "assistant")
            if n == 0:
                return json.dumps({"tool": "bridge_post", "args": {
                    "text": "rtl \u202e end " + ("e\u0301" * 40), "kind": "finding"}})
            if n == 1:
                return json.dumps({"tool": "bridge_read", "args": {}})
            return json.dumps({"final": "edge lane done"})

    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 4
    s.agent_timeout = 0
    reg = ToolRegistry()
    register_bridge_tools(reg)
    llm = LaneLLM()
    core = TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir), tools=reg)
    out = asyncio.run(core.squad("probe blackboard edges", n=1, run_id="lane1"))
    assert "edge lane done" in out["answer"] or "LEAD OK" in out["answer"]
    from avrone.bridge import for_core
    stored = for_core(core, "lane1").posts()
    assert stored and "\u202e" in stored[0]["text"] and "\u0301" in stored[0]["text"]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "at": stamp,
        "lane": "stub",
        "reason": "ollama binary is not installed on this VM",
        "run_id": out.get("run_id"),
        "answer": out.get("answer", "")[:500],
        "tests_produced": [
            "stub lane bridge_post RTL + combining flood",
            "stub lane bridge_read of that post",
        ],
        "post_id": stored[0]["id"],
    }
    folder = __import__("pathlib").Path("/opt/cursor/artifacts/edge-round")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "stub-lane.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logging.getLogger("avrone.bridge").warning("artifact write failed at %s: %s", stamp, exc)
    assert "\u202e" in stored[0]["text"]


def test_lead_crash_returns_raw_reports(tmp_path):
    class LeadBoom(FakeLLM):
        async def chat(self, messages, **kw):
            if "You are the LEAD" in messages[0]["content"]:
                raise KeyError("choices")
            return await super().chat(messages, **kw)

    core = _core(tmp_path, LeadBoom(PLAN3))
    out = asyncio.run(core.squad("task", n=3, run_id="r7"))
    assert out["answer"].startswith("Lead failed") and out["answer"].count("worker ok") == 3
