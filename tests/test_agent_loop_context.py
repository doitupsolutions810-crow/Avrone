"""Round 18: out-of-turns answers and context-window budget in the agent loop."""
import asyncio

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import (ELIDED, TrinityCore, compact_messages, context_chars,
                            out_of_turns_result)


class ScriptLLM:
    def __init__(self, replies): self.replies = list(replies); self.sizes = []; self.seen = []
    async def chat(self, messages, **kw):
        self.sizes.append(sum(len(m["content"]) for m in messages))
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else '{"final": "ran out"}'


def _core(tmp_path, replies, steps=8):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.max_steps = steps
    llm = ScriptLLM(replies)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir)), llm


def _kinds(core):
    return [e["kind"] for e in core.codex.replay("r1")]


CALL = '{"tool": "shell", "args": {"cmd": "echo hi"}}'


# --- out of turns -----------------------------------------------------------------------------

@pytest.mark.parametrize("reply,want", [
    ("plain summary", "plain summary"),
    ('{"final": "json final"}', "json final"),
    ('```json\n{"final": "fenced"}\n```', "fenced"),
    ('{"final": {"a": 1}}', '{"a": 1}'),
    (None, "[W ran out of turns (3 steps) without a final answer]"),
    ("   ", "[W ran out of turns (3 steps) without a final answer]"),
    (CALL, "[W ran out of turns (3 steps) without a final answer]"),
    ('{"final": null}', "[W ran out of turns (3 steps) without a final answer]"),
    ({"content": "from dict"}, "from dict"),
])
def test_out_of_turns_result(reply, want):
    assert out_of_turns_result(reply, "W", 3) == want


def test_loop_out_of_turns_unwraps_json_final(tmp_path):
    core, _ = _core(tmp_path, [CALL.replace("hi", f"n{i}") for i in range(3)] + ['{"final": "wrapped"}'], steps=3)
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "wrapped"
    assert "agent.out_of_turns" in _kinds(core)


def test_loop_out_of_turns_tool_call_is_not_the_result(tmp_path):
    core, _ = _core(tmp_path, [CALL.replace("hi", f"n{i}") for i in range(4)], steps=3)
    out = asyncio.run(core.run_agent("r1", "Worker", "x"))
    assert "ran out of turns" in out and '"tool"' not in out


def test_loop_out_of_turns_none_reply(tmp_path):
    core, _ = _core(tmp_path, [CALL.replace("hi", f"n{i}") for i in range(3)] + [None], steps=3)
    out = asyncio.run(core.run_agent("r1", "Worker", "x"))
    assert "ran out of turns" in out and "error" not in _kinds(core)


# --- context budget ---------------------------------------------------------------------------

@pytest.mark.parametrize("val,want", [(None, 120000), ("junk", 120000), ("10", 8000),
                                      ("999999999", 2000000), ("50000", 50000)])
def test_context_chars_env(monkeypatch, val, want):
    if val is None:
        monkeypatch.delenv("AVRONE_CONTEXT_CHARS", raising=False)
    else:
        monkeypatch.setenv("AVRONE_CONTEXT_CHARS", val)
    assert context_chars() == want


def _hist(n, body=5000):
    m = [{"role": "system", "content": "SYS" * 100}, {"role": "user", "content": "Begin."}]
    for i in range(n):
        m.append({"role": "assistant", "content": f'{{"tool": "shell", "args": {{"cmd": "c{i}"}}}}'})
        m.append({"role": "user", "content": f"TOOL RESULT (shell):\n{i}:" + "x" * body})
    return m


def test_compact_under_limit_is_noop():
    m = _hist(3); before = [dict(x) for x in m]
    assert compact_messages(m, 10**6) == 0 and m == before


def test_compact_elides_oldest_first_and_keeps_protected():
    m = _hist(10)
    n = compact_messages(m, 30000)
    assert n > 0
    assert sum(len(x["content"]) for x in m) <= 30000
    assert m[0]["content"] == "SYS" * 100 and m[1]["content"] == "Begin."
    assert ELIDED in m[3]["content"] and m[3]["content"].startswith("TOOL RESULT (shell)")
    assert all(ELIDED not in x["content"] for x in m[-4:])
    assert all(x["role"] != "assistant" or ELIDED not in x["content"] for x in m)


def test_compact_gives_up_when_only_protected_remain():
    m = [{"role": "system", "content": "S" * 50000}, {"role": "user", "content": "Begin."}]
    assert compact_messages(m, 8000) == 0
    assert len(m[0]["content"]) == 50000


def test_compact_skips_non_tool_user_turns():
    m = _hist(4)
    m.insert(2, {"role": "user", "content": "operator note " + "y" * 20000})
    compact_messages(m, 9000)
    assert m[2]["content"].startswith("operator note")


def test_loop_keeps_context_under_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CONTEXT_CHARS", "12000")
    monkeypatch.setenv("AVRONE_TOOL_OUTPUT_MAX", "5000")
    big = '{"tool": "shell", "args": {"cmd": "python3 -c \\"print(\'%s\'*6000)\\""}}'
    core, llm = _core(tmp_path, [big % c for c in "abcdef"] + ['{"final": "done"}'], steps=10)
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == "done"
    sys_prompt = llm.seen[0][0]["content"]
    assert all(h[0]["content"] == sys_prompt for h in llm.seen)   # system prompt never cut
    assert "context.compact" in _kinds(core)
    assert max(llm.sizes[3:]) < len(sys_prompt) + 12000 + 6000
