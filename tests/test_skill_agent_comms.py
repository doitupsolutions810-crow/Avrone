"""agent-comms-and-conclude skill: valid, names only real tools/env, and the conclude path works."""
import asyncio
import json
import re
from pathlib import Path

from avrone import bridge as bm
from avrone import evolve
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools
from avrone.trinity import TrinityCore

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "agent-comms-and-conclude" / "SKILL.md"


def test_skill_valid_and_grounded():
    body = SKILL.read_text()
    assert evolve.check_skill("agent-comms-and-conclude", body) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    names = set(reg.names())
    for tool in ("bridge_read", "bridge_post", "bridge_reply", "spawn", "research_note",
                 "lesson_recall", "router_status"):
        assert tool in body and tool in names, tool
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", body)):
        assert env in src, env


def call(tool, **args):
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


class ConcludeLLM:
    """Parent posts a finding, spawns a Verifier, reads the bridge; the Verifier reads the finding
    and replies with a conclusion that refs it."""

    def __init__(self):
        self.step = {"Researcher": 0, "Verifier": 0}

    async def chat(self, messages, **kw):
        role = str(kw.get("session", "")).rsplit(":", 1)[-1].split("-")[0]
        i = self.step.get(role, 0)
        self.step[role] = i + 1
        last = messages[-1]["content"] if messages else ""
        if role == "Researcher":
            seq = [call("bridge_post", text="queue latency comes from fsync per claim", kind="finding"),
                   call("spawn", role="Verifier", task="verify the queue latency finding", context="summary"),
                   call("bridge_read"),
                   '{"final": "concluded"}']
            return seq[min(i, 3)]
        if i == 0:
            return call("bridge_read", kind="finding")
        if i == 1:
            m = re.search(r"\[([0-9a-f]{12})\]", last)
            assert m, last
            return call("bridge_reply", refs=[m.group(1)], kind="conclusion", text="confirmed: batch fsync fixes it")
        return '{"final": "verified"}'


def test_parent_subagent_conclude_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 6
    s.max_spawn_depth = 2
    runner = LocalRunner(s)
    asyncio.run(runner.start())
    reg = builtin_registry()
    register_extra_tools(reg)
    core = TrinityCore(s, ConcludeLLM(), runner, Codex(s.data_dir), tools=reg)
    asyncio.run(core.run_agent("conc1", "Researcher", "find why the queue is slow"))
    posts = bm.for_core(core, "conc1").posts()
    finding = next(p for p in posts if p["kind"] == "finding")
    concl = next(p for p in posts if p["kind"] == "conclusion")
    assert finding["agent"].startswith("Researcher") and concl["agent"].startswith("Verifier")
    assert concl["refs"] == [finding["id"]]
    # the conclusion distills into a lesson the next run can recall
    assert bm.distill_lessons(s.data_dir, bm.for_core(core, "conc1"), "queue") 
    assert any("batch fsync" in v for v in bm.relevant_lessons(s.data_dir, "queue fsync latency"))
