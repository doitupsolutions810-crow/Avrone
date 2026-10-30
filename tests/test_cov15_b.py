"""Round 15 coverage: trinity - an agent that never stops calling tools is told it is out of turns and
its final plain answer is kept; a Lead merge that is far thinner than the reports is retried once."""
import asyncio
import json

from avrone.codex import Codex
from avrone.config import Settings
from avrone.llm import _mock_reply
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore


def _core(tmp_path, llm, steps=2):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = steps
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir)), s


class Looper:
    def __init__(self):
        self.last = []

    async def chat(self, messages, **kw):
        self.last.append(messages[-1]["content"])
        if messages[-1]["content"].startswith("Out of turns"):
            return "final after turns"
        return json.dumps({"tool": "no_such_tool", "args": {}})


def test_out_of_turns(tmp_path):
    llm = Looper()
    core, _ = _core(tmp_path, llm)
    r = asyncio.run(core.chat("hi", "r1"))
    assert r["answer"] == "final after turns"
    assert llm.last[-1].startswith("Out of turns") and len(llm.last) == 3


class ThinLead:
    def __init__(self):
        self.lead = 0

    async def chat(self, messages, **kw):
        if kw.get("lead"):
            self.lead += 1
            return "short" if self.lead == 1 else "Merged. " + "M" * 400
        if "agent in AVRONE, working inside" in messages[0]["content"]:
            return "F" * 1500
        return _mock_reply(messages)


def test_lead_retry_on_thin_merge(tmp_path):
    llm = ThinLead()
    core, s = _core(tmp_path, llm, steps=3)
    r = asyncio.run(core.squad("research x", 2, "r2"))
    assert llm.lead == 2 and r["answer"].startswith("Merged.")
    kinds = [json.loads(ln)["kind"] for ln in (s.data_dir / "runs" / "r2.jsonl").read_text().splitlines()]
    assert "lead.retry" in kinds
