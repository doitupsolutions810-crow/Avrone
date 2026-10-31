"""Round 20 coverage: a worker finding the bridge refuses (full board) is skipped and the
cross-exam still runs on the workers' results."""
import asyncio

from avrone import bridge as bridge_mod
from tests.test_bridge import ScriptLLM, _core


def test_refused_initial_finding_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")
    real = bridge_mod.Bridge.post

    def no_findings(self, agent, kind, text, **kw):
        if kind == "finding":
            raise bridge_mod.BridgeError("board full")
        return real(self, agent, kind, text, **kw)
    monkeypatch.setattr(bridge_mod.Bridge, "post", no_findings)
    llm = ScriptLLM(xexam=lambda label, rnd, u: '{"posts": [{"kind": "conclusion", "text": "ok"}]}')
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("t", n=3, run_id="nf"))
    assert out["answer"].startswith("LEAD MERGED") and len(llm.xcalls) == 3
    b = bridge_mod.for_core(core, "nf")
    assert b.read(kind="finding") == [] and len(b.read(kind="conclusion")) == 3
