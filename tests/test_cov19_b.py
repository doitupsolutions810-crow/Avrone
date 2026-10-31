"""Round 19 coverage: cross-examination skips a post the bridge refuses (full board, bad text)
without losing the round, and a cancelled cross-exam turn cancels the squad instead of being logged
as an ordinary turn failure."""
import asyncio

import pytest

from avrone import bridge as bridge_mod
from tests.test_bridge import ScriptLLM, _core


def test_refused_post_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")
    real = bridge_mod.Bridge.post

    def picky(self, agent, kind, text, **kw):
        if kind == "correction":
            raise bridge_mod.BridgeError("board full")
        return real(self, agent, kind, text, **kw)
    monkeypatch.setattr(bridge_mod.Bridge, "post", picky)
    llm = ScriptLLM(xexam=lambda label, rnd, u: '{"posts": [{"kind": "correction", "text": "no"}, '
                                                 '{"kind": "conclusion", "text": "agreed"}]}')
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("t", n=3, run_id="xr"))
    assert out["answer"].startswith("LEAD MERGED")
    assert out["bridge"]["corrections"] == 0  # refused corrections never count
    assert len(bridge_mod.for_core(core, "xr").read(kind="conclusion")) == 3


def test_cancelled_turn_propagates(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")
    llm = ScriptLLM(xexam=lambda label, rnd, u: asyncio.CancelledError() if label == "Agent2:B"
                    else '{"posts": []}')
    core = _core(tmp_path, llm)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core.squad("t", n=3, run_id="xc"))
