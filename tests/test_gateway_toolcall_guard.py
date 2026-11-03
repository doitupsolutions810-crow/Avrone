"""Round 19: cut-off, nameless, final-named, junk-trailed and duplicated native tool calls
from gpt-oss on Ollama. httpx.MockTransport only."""
import asyncio
import json

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.gateway import LLMError, tool_calls_as_action
from avrone.trinity import parse_action

MSG = [{"role": "system", "content": "agent"}, {"role": "user", "content": "hi"}]


def mk(monkeypatch, handler):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", "3")
    monkeypatch.setenv("AVRONE_LLM_BACKOFF", "0")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(Settings())


def teardown_function():
    gateway.HTTP_TRANSPORT = None


def resp(message, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})


def tc(name, args):
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}


def act(calls, finish=None):
    out = tool_calls_as_action(calls, finish)
    return json.loads(out) if out else None


# --- cut off mid-arguments --------------------------------------------------------------------

def test_truncated_call_is_never_dispatched(monkeypatch):
    hits = []
    def h(r):
        hits.append(1)
        return resp({"content": "", "tool_calls": [tc("shell", '{"cmd": "rm -rf /tmp/bu')]}, "length")
    g = mk(monkeypatch, h)
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "cut off mid-arguments" in str(e.value) and "nothing ran" in str(e.value)
    assert len(hits) == 1  # permanent: no retry burning the same tokens


def test_truncated_tail_dropped_complete_head_kept():
    calls = [tc("shell", '{"cmd": "ls"}'), tc("shell", '{"cmd": "make inst')]
    assert act(calls, "length") == {"tool": "shell", "args": {"cmd": "ls"}}


def test_unparseable_args_without_length_still_reach_arg_error():
    # not a truncation: the agent sees an ARG ERROR and can fix it (round 14 behavior)
    assert act([tc("shell", "cmd=ls")]) == {"tool": "shell", "args": {"_raw": "cmd=ls"}}


# --- junk after the arguments -----------------------------------------------------------------

@pytest.mark.parametrize("raw", ['{"cmd": "ls"}<|call|>', '{"cmd": "ls"}<|end|>',
                                 '<|constrain|>{"cmd": "ls"}', '{"cmd": "ls"} trailing',
                                 '{"cmd": "ls"}}', 'json {"cmd": "ls"}'])
def test_args_with_leaked_tokens_or_junk(raw):
    assert act([tc("shell", raw)], "length") == {"tool": "shell", "args": {"cmd": "ls"}}


# --- nameless calls ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", None, "   ", "functions.", "<|call|>"])
def test_nameless_call_carrying_an_action(name):
    calls = [tc(name, '{"tool": "functions.shell", "args": {"cmd": "pwd"}}')]
    assert act(calls) == {"tool": "shell", "args": {"cmd": "pwd"}}


def test_nameless_call_carrying_a_final():
    assert act([tc("", '{"final": "all done"}')]) == {"final": "all done"}


def test_nameless_call_with_plain_args_is_dropped():
    assert act([tc("", '{"cmd": "ls"}')]) is None


# --- final as a tool name ---------------------------------------------------------------------

@pytest.mark.parametrize("name,args,want", [
    ("final", '{"final": "ok"}', "ok"),
    ("functions.final_answer", '{"answer": "42"}', "42"),
    ("Final", '{"text": "t"}', "t"),
    ("final", '{"summary": "only key"}', "only key"),
    ("final", '{"a": 1, "b": 2}', {"a": 1, "b": 2}),
    ("final", "", {}),
])
def test_final_named_call_becomes_final(name, args, want):
    assert act([tc(name, args)]) == {"final": want}


def test_final_beside_tool_call_lets_the_tool_run_first():
    calls = [tc("shell", '{"cmd": "ls"}'), tc("final", '{"final": "done"}')]
    assert act(calls) == {"tool": "shell", "args": {"cmd": "ls"}}


# --- duplicates -------------------------------------------------------------------------------

def test_duplicate_calls_run_once():
    calls = [tc("shell", '{"cmd": "ls"}'), tc("functions.shell", {"cmd": "ls"}), tc("shell", '{"cmd": "pwd"}')]
    assert act(calls) == {"tools": [{"tool": "shell", "args": {"cmd": "ls"}},
                                    {"tool": "shell", "args": {"cmd": "pwd"}}]}


# --- end to end through chat() + parse_action --------------------------------------------------

def test_chat_final_named_call_parses_as_final(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": "", "tool_calls": [tc("final", '{"answer": "yes"}')]}))
    assert parse_action(asyncio.run(g.chat(MSG))) == {"final": "yes"}


def test_chat_junk_tail_under_length_still_runs(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": "", "tool_calls": [tc("shell", '{"cmd":"ls"}<|call|>')]},
                                       "length"))
    assert parse_action(asyncio.run(g.chat(MSG))) == {"tool": "shell", "args": {"cmd": "ls"}}
