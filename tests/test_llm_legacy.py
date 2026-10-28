"""Offline tests for llm.py: the legacy direct client, retry delays, and the mock model."""
import asyncio
import json

import httpx
import pytest

from avrone import llm as llm_mod
from avrone.config import Settings
from avrone.llm import LLM, LLMError, _mock_reply, _retry_delay


def run(c):
    return asyncio.run(c)


def make(**over):
    s = Settings()
    s.llm_mode = "live"
    s.provider = over.pop("provider", "openclaw")
    s.llm_api_key = over.pop("key", "sk-test")
    s.llm_base_url = "http://model.local/v1/"
    s.llm_model = "m1"
    s.llm_retries = over.pop("retries", 3)
    s.llm_backoff = 0.0
    s.llm_timeout = 5
    for k, v in over.items():
        setattr(s, k, v)
    return LLM(s)


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = httpx.Headers(headers or {})
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def ok(content="hello"):
    return Resp(200, {"choices": [{"message": {"content": content}}]})


def install(monkeypatch, script):
    """script: list of Resp or Exception, consumed per POST. Returns the recorded calls."""
    calls = []
    it = iter(script)

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return item
    monkeypatch.setattr(httpx, "AsyncClient", C)

    async def nosleep(_):
        return None
    monkeypatch.setattr(llm_mod.asyncio, "sleep", nosleep)
    return calls


def test_success_headers_body(monkeypatch):
    calls = install(monkeypatch, [ok("hi there")])
    out = run(make()._legacy_chat([{"role": "user", "content": "x"}], session="agent-1", temperature=0.2))
    assert out == "hi there"
    c = calls[0]
    assert c["url"] == "http://model.local/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer sk-test"
    assert c["headers"]["x-openclaw-session-key"] == "agent-1"
    assert c["json"] == {"model": "m1", "messages": [{"role": "user", "content": "x"}], "temperature": 0.2}


def test_no_key_no_auth_header_and_openrouter_title(monkeypatch):
    calls = install(monkeypatch, [ok()])
    run(make(provider="openrouter", key="")._legacy_chat([], session="s", model="other"))
    h = calls[0]["headers"]
    assert "Authorization" not in h and h["X-Title"] == "AVRONE"
    assert "x-openclaw-session-key" not in h  # session header only for openclaw/custom
    assert calls[0]["json"]["model"] == "other"


def test_null_content_becomes_empty(monkeypatch):
    install(monkeypatch, [ok(None)])
    assert run(make()._legacy_chat([])) == ""


def test_retries_then_succeeds(monkeypatch):
    calls = install(monkeypatch, [Resp(429, text="slow down"), httpx.ReadTimeout("t"), ok("finally")])
    assert run(make(retries=3)._legacy_chat([])) == "finally"
    assert len(calls) == 3


def test_retries_exhausted(monkeypatch):
    install(monkeypatch, [Resp(503, text="down")] * 2)
    with pytest.raises(LLMError, match=r"503.*after 2 tries"):
        run(make(retries=2)._legacy_chat([]))


def test_retries_floor_of_one(monkeypatch):
    calls = install(monkeypatch, [Resp(502, text="x")])
    with pytest.raises(LLMError, match="after 1 tries"):
        run(make(retries=0)._legacy_chat([]))
    assert len(calls) == 1


def test_connect_error_not_retried(monkeypatch):
    calls = install(monkeypatch, [httpx.ConnectError("refused"), ok()])
    with pytest.raises(LLMError, match="unreachable"):
        run(make()._legacy_chat([]))
    assert len(calls) == 1


def test_non_retryable_4xx(monkeypatch):
    calls = install(monkeypatch, [Resp(401, text="bad key")])
    with pytest.raises(LLMError, match="401: bad key"):
        run(make()._legacy_chat([]))
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{}]}, ValueError("html"), ["x"]])
def test_malformed_response(monkeypatch, body):
    install(monkeypatch, [Resp(200, body, text="<html>")])
    with pytest.raises(LLMError, match="Unexpected model response"):
        run(make()._legacy_chat([]))


def test_mock_mode_short_circuits(monkeypatch):
    install(monkeypatch, [])  # any POST would raise StopIteration
    lm = make(llm_mode="mock")
    assert "tool" in run(lm._legacy_chat([{"role": "user", "content": "hi"}]))


# ------------------------------------------------------------ retry delay
def test_retry_delay():
    assert _retry_delay(None, 0.5, 0) == 0.5
    assert _retry_delay(None, 0.5, 3) == 4.0
    r = Resp(429, {}, headers={"retry-after": "10"})
    assert _retry_delay(r, 0.5, 0) == 10.0
    assert _retry_delay(Resp(429, {}, headers={"retry-after": "999"}), 0.5, 0) == 30.0  # capped
    assert _retry_delay(Resp(429, {}, headers={"retry-after": "Wed, 21 Oct"}), 1.0, 1) == 2.0
    assert _retry_delay(Resp(429, {}, headers={"retry-after": "1"}), 1.0, 3) == 8.0  # backoff wins


# ------------------------------------------------------------ mock model branches
def test_mock_planner():
    out = json.loads(_mock_reply([{"content": "You are the PLANNER"}, {"content": "TASK: build x"}]))
    assert [s["role"] for s in out["subtasks"]] == ["Researcher", "Engineer", "Critic"]
    assert "build x" in out["subtasks"][0]["focus"]
    out = json.loads(_mock_reply([{"content": "You are the PLANNER"}, {"content": "no marker"}]))
    assert "the task" in out["subtasks"][0]["focus"]


def test_mock_deep_lead_counts_reviewers():
    out = _mock_reply([{"content": "You are the DEEP LEAD"}, {"content": "### Reviewer 1\n### Reviewer 2"}])
    assert "2 reviewers" in out and "## Verdict" in out


def test_mock_lead_and_workers():
    assert "FINAL ANSWER" in _mock_reply([{"content": "You are the LEAD"}, {"content": "x"}])
    assert '"tool": "shell"' in _mock_reply([{"content": "worker"}, {"content": "go"}])
    assert "Mock worker result" in _mock_reply([{"content": "worker"}, {"content": "TOOL RESULT ok"}])
    deep = json.loads(_mock_reply([{"content": "DEEP ANALYSIS wave: you are the Contrarian reviewer"},
                                   {"content": "TOOL RESULT"}]))
    assert "Contrarian" in deep["final"]
    deep = json.loads(_mock_reply([{"content": "DEEP ANALYSIS wave"}, {"content": "TOOL RESULT"}]))
    assert "deep" in deep["final"]


def test_mock_empty_messages():
    assert "tool" in _mock_reply([])


def test_status_and_chat_delegate_to_gateway(monkeypatch):
    lm = make()

    async def fake_chat(messages, model, temperature, session, lead):
        return f"gw:{model}:{lead}"
    monkeypatch.setattr(lm.gateway, "chat", fake_chat)
    monkeypatch.setattr(lm.gateway, "status", lambda: {"ok": True})
    assert run(lm.chat([], model="x", lead=True)) == "gw:x:True"
    assert lm.status() == {"ok": True}
