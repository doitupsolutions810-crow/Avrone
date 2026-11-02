"""gpt-oss on Ollama answers 500 'error parsing tool call' when the model writes into its tool
channel. The gateway retries with an explicit plain-text instruction instead of resending the same
request. httpx.MockTransport only."""
import asyncio
import json

import httpx

from avrone import gateway
from avrone.config import Settings

MSG = [{"role": "system", "content": "You are an agent."}, {"role": "user", "content": "hi"}]
ERR = {"error": "error parsing tool call: raw='{\"tool\":\"shell\"', err=unexpected end of JSON input"}


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


def ok(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_retry_after_tool_parse_500_carries_the_plain_text_nudge(monkeypatch):
    bodies = []

    def handler(req):
        b = json.loads(req.content); bodies.append(b)
        if gateway.PLAIN_TEXT_NUDGE in b["messages"][0]["content"]:
            return ok('{"tool":"shell","args":{}}')
        return httpx.Response(500, json=ERR)
    g = mk(monkeypatch, handler)
    assert asyncio.run(g.chat(MSG)) == '{"tool":"shell","args":{}}'
    assert len(bodies) == 2
    assert gateway.PLAIN_TEXT_NUDGE not in bodies[0]["messages"][0]["content"]
    assert bodies[1]["messages"][0]["content"].startswith("You are an agent.")
    assert len(bodies[1]["messages"]) == 2  # nudge goes into the system prompt, not a new turn


def test_caller_messages_are_never_mutated(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500, json=ERR) if len(calls) == 1 else ok("fine")
    msgs = [dict(m) for m in MSG]
    g = mk(monkeypatch, handler)
    asyncio.run(g.chat(msgs))
    assert msgs == MSG


def test_no_system_prompt_gets_one(monkeypatch):
    seen = []

    def handler(req):
        b = json.loads(req.content); seen.append(b["messages"])
        return httpx.Response(500, json=ERR) if len(seen) == 1 else ok("x")
    g = mk(monkeypatch, handler)
    asyncio.run(g.chat([{"role": "user", "content": "hi"}]))
    assert seen[1][0] == {"role": "system", "content": gateway.PLAIN_TEXT_NUDGE}
    assert seen[1][1] == {"role": "user", "content": "hi"}


def test_nudge_is_added_only_once_across_retries(monkeypatch):
    seen = []

    def handler(req):
        b = json.loads(req.content); seen.append(b["messages"][0]["content"])
        return httpx.Response(500, json=ERR) if len(seen) < 3 else ok("x")
    g = mk(monkeypatch, handler)
    assert asyncio.run(g.chat(MSG)) == "x"
    assert seen[2].count(gateway.PLAIN_TEXT_NUDGE) == 1


def test_plain_500_does_not_nudge(monkeypatch):
    seen = []

    def handler(req):
        b = json.loads(req.content); seen.append(b["messages"][0]["content"])
        return httpx.Response(500, text="model runner crashed") if len(seen) == 1 else ok("x")
    g = mk(monkeypatch, handler)
    asyncio.run(g.chat(MSG))
    assert gateway.PLAIN_TEXT_NUDGE not in seen[1]


def test_persistent_tool_parse_500_still_fails_cleanly(monkeypatch):
    g = mk(monkeypatch, lambda req: httpx.Response(500, json=ERR))
    try:
        asyncio.run(g.chat(MSG))
    except gateway.LLMError as e:
        assert "ollama 500" in str(e) and "parsing tool call" in str(e)
    else:
        raise AssertionError("should fail")


def test_classifier():
    assert gateway.is_tool_parse_error(500, json.dumps(ERR))
    assert gateway.is_tool_parse_error(500, "Harmony parser: bad channel")
    assert not gateway.is_tool_parse_error(400, json.dumps(ERR))
    assert not gateway.is_tool_parse_error(500, "")
    assert not gateway.is_tool_parse_error(503, "overloaded")
