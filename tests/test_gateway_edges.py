"""Offline edge cases for gateway fallback parsing and routing."""
import asyncio

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings


@pytest.fixture(autouse=True)
def reset_transport():
    yield
    gateway.HTTP_TRANSPORT = None


def make_gateway(monkeypatch, handler, **values):
    defaults = {
        "AVRONE_PROVIDER": "xai",
        "AVRONE_LLM_API_KEY": "test-key",
        "AVRONE_LLM_FALLBACKS": "ollama:llama3.1",
        "AVRONE_LLM_MODE": "live",
        "AVRONE_LLM_RETRIES": "1",
        "AVRONE_LLM_BACKOFF": "0",
        "AVRONE_BREAKER_THRESHOLD": "2",
    }
    defaults.update(values)
    for name, value in defaults.items():
        monkeypatch.setenv(name, value)
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(Settings())


def reply(text="ok"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_fallback_parser_skips_invalid_entries_and_keeps_lead_model(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "xai")
    monkeypatch.setenv("AVRONE_LLM_API_KEY", "key")
    monkeypatch.setenv(
        "AVRONE_LLM_FALLBACKS",
        "bogus:model,custom:missing-url,openrouter:x-ai/grok-4|lead-x,ollama:llama3.1,,",
    )
    s = Settings()
    upstreams = gateway.parse_upstreams(s)
    assert [u.provider for u in upstreams] == ["xai", "openrouter", "ollama"]
    assert upstreams[1].model == "x-ai/grok-4"
    assert upstreams[1].lead_model == "lead-x"


def test_permanent_primary_error_fails_over_without_retries(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.x.ai":
            return httpx.Response(401, text="bad credentials")
        return reply("fallback worked")

    g = make_gateway(monkeypatch, handler, AVRONE_LLM_RETRIES="3")
    out = asyncio.run(g.chat([{"role": "user", "content": "hello"}]))
    assert out == "fallback worked"
    assert seen.count("api.x.ai") == 1
    assert len(seen) == 2
    assert g.last_route == ["xai:fail", "ollama:ok"]


def test_malformed_success_response_is_retried_then_fails_over(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.x.ai":
            return httpx.Response(200, json={"choices": []})
        return reply("valid fallback")

    g = make_gateway(monkeypatch, handler, AVRONE_LLM_RETRIES="1")
    out = asyncio.run(g.chat([{"role": "user", "content": "hello"}]))
    assert out == "valid fallback"
    assert seen == ["api.x.ai", "127.0.0.1"]
    assert g.last_route == ["xai:fail", "ollama:ok"]


def test_explicit_model_overrides_lead_model(monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen["model"] = json.loads(request.content)["model"]
        return reply()

    g = make_gateway(
        monkeypatch, handler,
        AVRONE_LEAD_MODEL="lead-model",
        AVRONE_LLM_MODEL="worker-model",
    )
    asyncio.run(g.chat([{"role": "user", "content": "x"}], model="explicit-model", lead=True))
    assert seen["model"] == "explicit-model"
