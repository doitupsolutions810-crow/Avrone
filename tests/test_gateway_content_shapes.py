"""Round 20: content-part lists, prose preambles beside native tool calls, refusals and
double-encoded tool arguments from OpenAI-compatible servers (Ollama /v1, llama.cpp, vLLM).
httpx.MockTransport only."""
import asyncio
import json

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.gateway import content_text, tool_calls_as_action
from avrone.trinity import parse_action

MSG = [{"role": "system", "content": "agent"}, {"role": "user", "content": "hi"}]


def mk(monkeypatch, handler):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", "2")
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


def chat(monkeypatch, message, finish="stop"):
    return asyncio.run(mk(monkeypatch, lambda r: resp(message, finish)).chat(MSG))


# --- content parts ----------------------------------------------------------------------------

@pytest.mark.parametrize("content,want", [
    ([{"type": "text", "text": '{"final": "ok"}'}], '{"final": "ok"}'),
    ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "ab"),
    ([{"type": "output_text", "text": "x"}], "x"),
    ([{"type": "text", "text": {"value": "v"}}], "v"),
    ([{"text": "untyped"}], "untyped"),
    ([{"type": "image_url", "image_url": {"url": "u"}}, {"type": "text", "text": "t"}], "t"),
    (["s1", "s2"], "s1s2"),
    ({"type": "text", "text": "single"}, "single"),
    ([], ""),
    ([None, 3, {"type": "text"}], ""),
    (None, ""),
    (b"bytes \xff", "bytes \ufffd"),
    (42, "42"),
])
def test_content_text_shapes(content, want):
    assert content_text(content) == want


def test_parts_list_reaches_the_agent_as_text_not_a_repr(monkeypatch):
    out = chat(monkeypatch, {"content": [{"type": "text", "text": '{"tool": "shell", "args": {"cmd": "ls"}}'}]})
    assert "'type'" not in out
    assert parse_action(out) == {"tool": "shell", "args": {"cmd": "ls"}}


def test_parts_list_of_only_images_falls_through_to_tool_calls(monkeypatch):
    out = chat(monkeypatch, {"content": [{"type": "image_url", "image_url": {}}],
                             "tool_calls": [tc("shell", '{"cmd": "pwd"}')]})
    assert json.loads(out) == {"tool": "shell", "args": {"cmd": "pwd"}}


# --- prose preamble beside a tool call ---------------------------------------------------------

@pytest.mark.parametrize("pre", ["Let me list the files.", "<|channel|>commentary", "  ok \n",
                                 "<think>I should run ls</think>Running it now.",
                                 [{"type": "text", "text": "Checking..."}]])
def test_prose_preamble_does_not_end_the_agent(monkeypatch, pre):
    out = chat(monkeypatch, {"content": pre, "tool_calls": [tc("functions.shell", '{"cmd": "ls"}')]})
    assert parse_action(out) == {"tool": "shell", "args": {"cmd": "ls"}}



def test_prose_kept_when_tool_calls_unusable(monkeypatch):
    out = chat(monkeypatch, {"content": "Here is the answer.", "tool_calls": [{"function": {"name": ""}}]})
    assert out == "Here is the answer."


def test_prose_kept_when_tool_call_was_cut_off(monkeypatch):
    out = chat(monkeypatch, {"content": "Partial thought", "tool_calls": [tc("shell", '{"cmd": "rm -')]}, "length")
    assert out == "Partial thought"


def test_prose_without_tool_calls_is_unchanged(monkeypatch):
    assert chat(monkeypatch, {"content": "just prose", "tool_calls": []}) == "just prose"


# --- refusal ----------------------------------------------------------------------------------

def test_refusal_with_empty_content_is_returned(monkeypatch):
    assert chat(monkeypatch, {"content": None, "refusal": "I can't help with that."}) == "I can't help with that."


def test_tool_call_beats_refusal(monkeypatch):
    out = chat(monkeypatch, {"content": "", "refusal": "no", "tool_calls": [tc("shell", "{}")]})
    assert json.loads(out) == {"tool": "shell", "args": {}}


@pytest.mark.parametrize("ref", [None, "", "  ", 5])
def test_blank_refusal_ignored(monkeypatch, ref):
    assert chat(monkeypatch, {"content": "", "refusal": ref}) == ""


# --- double-encoded arguments ------------------------------------------------------------------

@pytest.mark.parametrize("raw", [json.dumps(json.dumps({"cmd": "ls"})),
                                 json.dumps(json.dumps(json.dumps({"cmd": "ls"})))])
def test_double_encoded_arguments(raw):
    assert json.loads(tool_calls_as_action([tc("shell", raw)])) == {"tool": "shell", "args": {"cmd": "ls"}}


def test_plain_string_argument_is_not_unwrapped():
    out = json.loads(tool_calls_as_action([tc("shell", json.dumps("ls -la"))]))
    assert out == {"tool": "shell", "args": {"_raw": "ls -la"}}


def test_double_encoded_garbage_stays_raw():
    out = json.loads(tool_calls_as_action([tc("shell", json.dumps("{not json"))]))
    assert out == {"tool": "shell", "args": {"_raw": "{not json"}}
