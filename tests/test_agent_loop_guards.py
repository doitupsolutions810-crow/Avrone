"""Round 17: agent-loop failure classes seen with local models.
Empty turns ended the agent with nothing, a non-text reply crashed it, an identical call could
repeat until max_steps, and a huge tool result went into the context uncut."""
import asyncio

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import (REPEAT_CALL_LIMIT, TrinityCore, clip_output, reply_text,
                            tool_output_max)


class ScriptLLM:
    def __init__(self, replies): self.replies = list(replies); self.seen = []
    async def chat(self, messages, **kw):
        self.seen.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else '{"final": "ran out"}'


def _core(tmp_path, replies, steps=8):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.max_steps = steps
    llm = ScriptLLM(replies)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir)), llm


def _kinds(core):
    return [e["kind"] for e in core.codex.replay("r1")]


# --- empty turns ------------------------------------------------------------------------------

@pytest.mark.parametrize("empty", ["", "   ", "\n\n", None])
def test_empty_turn_is_nudged_then_recovers(tmp_path, empty):
    core, llm = _core(tmp_path, [empty, '{"final": "real answer"}'])
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "real answer"
    assert "action.empty" in _kinds(core)
    assert "empty" in llm.seen[-1].lower()


def test_endless_empty_turns_still_terminate(tmp_path):
    core, llm = _core(tmp_path, [""] * 20)
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == ""
    assert len(llm.seen) == 3                     # 1 + 2 nudges, then it stops
    assert _kinds(core).count("action.empty") == 2


# --- non-text replies -------------------------------------------------------------------------

@pytest.mark.parametrize("reply,want", [
    (None, ""),
    (b'{"final": "bytes"}', '{"final": "bytes"}'),
    ({"content": "hi"}, "hi"),
    ({"message": {"content": "nested"}}, "nested"),
    ({"role": "assistant"}, '{"role": "assistant"}'),
    (42, "42"),
])
def test_reply_text(reply, want):
    assert reply_text(reply) == want


def test_loop_survives_non_text_reply(tmp_path):
    core, _ = _core(tmp_path, [{"content": '{"final": "from dict"}'}])
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "from dict"
    assert "error" not in _kinds(core)


# --- repeated identical calls -----------------------------------------------------------------

CALL = '{"tool": "shell", "args": {"cmd": "echo same-out"}}'


def test_identical_call_loop_is_flagged(tmp_path):
    core, llm = _core(tmp_path, [CALL] * REPEAT_CALL_LIMIT + ['{"final": "stopped"}'])
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "stopped"
    assert "identical call" in llm.seen[-1]
    assert _kinds(core).count("tool.repeat") == 1


def test_varied_calls_are_not_flagged(tmp_path):
    calls = [f'{{"tool": "shell", "args": {{"cmd": "echo n{i}"}}}}' for i in range(4)]
    core, llm = _core(tmp_path, calls + ['{"final": "ok"}'])
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "ok"
    assert "tool.repeat" not in _kinds(core)
    assert not any("identical call" in m for m in llm.seen)


def test_repeat_counter_resets_after_a_different_call(tmp_path):
    other = '{"tool": "shell", "args": {"cmd": "echo other"}}'
    core, _ = _core(tmp_path, [CALL, CALL, other, CALL, CALL, '{"final": "ok"}'])
    asyncio.run(core.run_agent("r1", "Worker", "x"))
    assert "tool.repeat" not in _kinds(core)


# --- output cap -------------------------------------------------------------------------------

def test_clip_keeps_head_and_tail():
    out = "H" * 5000 + "M" * 50000 + "TAIL-ERROR"
    c = clip_output(out, 3000)
    assert c.startswith("H") and c.endswith("TAIL-ERROR")
    assert "chars cut" in c and len(c) < 3200
    assert clip_output("short", 3000) == "short"


@pytest.mark.parametrize("val,want", [(None, 20000), ("junk", 20000), ("5", 1000),
                                      ("99999999", 500000), ("4000", 4000)])
def test_tool_output_max_env(monkeypatch, val, want):
    if val is None:
        monkeypatch.delenv("AVRONE_TOOL_OUTPUT_MAX", raising=False)
    else:
        monkeypatch.setenv("AVRONE_TOOL_OUTPUT_MAX", val)
    assert tool_output_max() == want


def test_huge_tool_output_is_clipped_in_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_TOOL_OUTPUT_MAX", "2000")
    core, llm = _core(tmp_path, [
        '{"tool": "shell", "args": {"cmd": "python3 -c \\"print(\'x\'*60000); print(\'END-MARK\')\\""}}',
        '{"final": "ok"}'])
    asyncio.run(core.run_agent("r1", "Worker", "x"))
    res = llm.seen[-1]
    assert len(res) < 2600
