"""gpt-oss on Ollama can return the action as a parsed native tool call (empty content plus
message.tool_calls), or spend every token on reasoning (empty content, finish_reason=length).
httpx.MockTransport only."""
import asyncio
import json

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.trinity import parse_action

MSG = [{"role": "system", "content": "agent"}, {"role": "user", "content": "hi"}]


def mk(monkeypatch, handler, retries="3"):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", retries)
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


@pytest.mark.parametrize("args", ['{"cmd": "ls"}', {"cmd": "ls"}])
def test_native_tool_call_becomes_json_action(monkeypatch, args):
    g = mk(monkeypatch, lambda r: resp({"role": "assistant", "content": "", "tool_calls": [tc("shell", args)]}))
    out = asyncio.run(g.chat(MSG))
    assert json.loads(out) == {"tool": "shell", "args": {"cmd": "ls"}}
    assert parse_action(out)["tool"] == "shell"


@pytest.mark.parametrize("raw", ["functions.shell", "repo_browser.shell",
                                 "assistant<|channel|>commentary to=functions.shell"])
def test_gpt_oss_namespaced_names(monkeypatch, raw):
    g = mk(monkeypatch, lambda r: resp({"content": None, "tool_calls": [tc(raw, "{}")]}))
    assert json.loads(asyncio.run(g.chat(MSG))) == {"tool": "shell", "args": {}}


def test_several_tool_calls_become_a_batch(monkeypatch):
    calls = [tc("shell", '{"cmd":"a"}'), tc("fs_read", '{"path":"b"}')]
    g = mk(monkeypatch, lambda r: resp({"content": "", "tool_calls": calls}))
    assert json.loads(asyncio.run(g.chat(MSG))) == {"tools": [
        {"tool": "shell", "args": {"cmd": "a"}}, {"tool": "fs_read", "args": {"path": "b"}}]}


def test_bad_arguments_are_kept_not_dropped(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": "", "tool_calls": [tc("shell", '{"cmd": "ls"')]}))
    assert json.loads(asyncio.run(g.chat(MSG))) == {"tool": "shell", "args": {"_raw": '{"cmd": "ls"'}}


def test_text_content_wins_over_tool_calls(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": "plain", "tool_calls": [tc("shell", "{}")]}))
    assert asyncio.run(g.chat(MSG)) == "plain"


@pytest.mark.parametrize("calls", [None, [], "x", [{"function": {"name": ""}}], [{"nope": 1}], [None]])
def test_junk_tool_calls_give_empty_reply(monkeypatch, calls):
    g = mk(monkeypatch, lambda r: resp({"content": "", "tool_calls": calls}))
    assert asyncio.run(g.chat(MSG)) == ""


def test_truncated_empty_reply_fails_fast_with_reason(monkeypatch):
    n = []

    def h(r):
        n.append(1)
        return resp({"content": "", "reasoning": "thinking..." * 50}, finish="length")
    g = mk(monkeypatch, h)
    with pytest.raises(gateway.LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "finish_reason=length" in str(e.value)
    assert len(n) == 1  # not retried: the same request truncates the same way


def test_truncated_but_partial_text_is_returned(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": '{"tool": "shell"'}, finish="length"))
    assert asyncio.run(g.chat(MSG)) == '{"tool": "shell"'


def test_message_not_a_dict_is_a_bad_response(monkeypatch):
    g = mk(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": "hi"}]}), retries="1")
    with pytest.raises(gateway.LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "bad response" in str(e.value)


def test_non_string_content_is_stringified(monkeypatch):
    g = mk(monkeypatch, lambda r: resp({"content": 42}))
    assert asyncio.run(g.chat(MSG)) == "42"
