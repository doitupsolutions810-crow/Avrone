"""Offline edge tests for avrone.gateway: headers per provider, malformed replies, network
errors and their failover, and fallback specs that cannot resolve. httpx.MockTransport only."""
import asyncio

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings

MSG = [{"role": "user", "content": "hi"}]


def mk(monkeypatch, handler, **env):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(Settings())


def teardown_function():
    gateway.HTTP_TRANSPORT = None


def reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_custom_fallback_without_base_url_is_skipped(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "xai")
    monkeypatch.setenv("AVRONE_LLM_API_KEY", "k")
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    ups = gateway.parse_upstreams(Settings(), env={"AVRONE_LLM_FALLBACKS": " custom:m , ,ollama|big-lead"})
    assert [u.provider for u in ups] == ["xai", "ollama"]
    assert ups[1].model == "llama3.1" and ups[1].lead_model == "big-lead"


def test_openclaw_session_header_and_auth(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return reply("ok")
    g = mk(monkeypatch, handler, AVRONE_PROVIDER="openclaw", AVRONE_LLM_API_KEY="tok",
           AVRONE_LLM_RETRIES="1", AVRONE_LLM_BACKOFF="0")
    assert asyncio.run(g.chat(MSG, session="sess-42")) == "ok"
    assert seen["x-openclaw-session-key"] == "sess-42"
    assert seen["authorization"] == "Bearer tok"
    assert "x-title" not in seen


def test_openrouter_title_header_and_no_session_leak(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return reply("ok")
    g = mk(monkeypatch, handler, AVRONE_PROVIDER="openrouter", AVRONE_LLM_API_KEY="k",
           AVRONE_LLM_RETRIES="1", AVRONE_LLM_BACKOFF="0")
    asyncio.run(g.chat(MSG, session="secret-session"))
    assert seen["x-title"] == "AVRONE"
    assert "x-openclaw-session-key" not in seen  # session keys only go to openclaw/custom


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json=["list"]),
])
def test_malformed_200_is_retryable_then_fails_over(monkeypatch, resp):
    def handler(req):
        if "x.ai" in str(req.url):
            return resp
        return reply("from-ollama")
    g = mk(monkeypatch, handler, AVRONE_PROVIDER="xai", AVRONE_LLM_API_KEY="k",
           AVRONE_LLM_FALLBACKS="ollama", AVRONE_LLM_RETRIES="1", AVRONE_LLM_BACKOFF="0")
    assert asyncio.run(g.chat(MSG)) == "from-ollama"
    assert g.last_route == ["xai:fail", "ollama:ok"]


def test_null_content_becomes_empty_string(monkeypatch):
    g = mk(monkeypatch, lambda req: httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
           AVRONE_PROVIDER="xai", AVRONE_LLM_API_KEY="k", AVRONE_LLM_RETRIES="1")
    assert asyncio.run(g.chat(MSG)) == ""


def test_connect_error_and_read_timeout_are_reported(monkeypatch):
    def handler(req):
        if "x.ai" in str(req.url):
            raise httpx.ConnectError("refused", request=req)
        raise httpx.ReadTimeout("too slow", request=req)
    g = mk(monkeypatch, handler, AVRONE_PROVIDER="xai", AVRONE_LLM_API_KEY="k",
           AVRONE_LLM_FALLBACKS="ollama", AVRONE_LLM_RETRIES="2", AVRONE_LLM_BACKOFF="0")
    with pytest.raises(gateway.LLMError) as ei:
        asyncio.run(g.chat(MSG))
    msg = str(ei.value)
    assert "xai unreachable: refused" in msg and "ollama ReadTimeout: too slow" in msg
    assert g.last_route == ["xai:fail", "ollama:fail"]
    xai = g.upstreams[0]
    assert xai.calls == 2 and xai.errors == 2  # primary retried on network errors too


def test_permanent_error_skips_retries(monkeypatch):
    hits = []

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(401, text="bad key")
    g = mk(monkeypatch, handler, AVRONE_PROVIDER="xai", AVRONE_LLM_API_KEY="k",
           AVRONE_LLM_RETRIES="3", AVRONE_LLM_BACKOFF="0")
    with pytest.raises(gateway.LLMError, match="xai 401: bad key"):
        asyncio.run(g.chat(MSG))
    assert len(hits) == 1


def test_all_open_breakers_still_try(monkeypatch):
    g = mk(monkeypatch, lambda req: reply("revived"), AVRONE_PROVIDER="xai", AVRONE_LLM_API_KEY="k",
           AVRONE_LLM_RETRIES="1")
    for u in g.upstreams:
        u.open_until = 1e18
    assert asyncio.run(g.chat(MSG)) == "revived"
    assert g.upstreams[0].status()["state"] == "closed"  # success resets the breaker
