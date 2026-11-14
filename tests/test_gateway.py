import asyncio, httpx, pytest
from avrone import gateway
from avrone.config import Settings


def mk(settings_env, monkeypatch, handler):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    for k, v in settings_env.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(s)


def teardown_function():
    gateway.HTTP_TRANSPORT = None


def reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_parse_fallbacks(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "xai")
    monkeypatch.setenv("AVRONE_LLM_API_KEY", "k")
    monkeypatch.setenv("AVRONE_LLM_FALLBACKS", "openrouter:x-ai/grok-4,ollama:llama3.1,bogus:x")
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    ups = gateway.parse_upstreams(Settings())
    assert [u.provider for u in ups] == ["xai", "openrouter", "ollama"]
    assert ups[2].base_url.startswith("http://127.0.0.1:11434")


def test_failover_then_breaker(monkeypatch):
    hits = {"xai": 0, "ollama": 0}

    def handler(req):
        if "x.ai" in str(req.url):
            hits["xai"] += 1
            return httpx.Response(429, text="slow down")
        hits["ollama"] += 1
        return reply("from-ollama")

    g = mk({"AVRONE_PROVIDER": "xai", "AVRONE_LLM_API_KEY": "k", "AVRONE_LLM_FALLBACKS": "ollama:llama3.1",
            "AVRONE_LLM_RETRIES": "2", "AVRONE_LLM_BACKOFF": "0", "AVRONE_BREAKER_THRESHOLD": "3"}, monkeypatch, handler)
    out = asyncio.run(g.chat([{"role": "user", "content": "hi"}]))
    assert out == "from-ollama"
    assert g.last_route == ["xai:fail", "ollama:ok"]
    assert hits["xai"] == 2  # retried primary twice before failover
    # primary tripped its breaker (2 fails) ... one more call opens it and it's skipped
    asyncio.run(g.chat([{"role": "user", "content": "again"}]))
    before = hits["xai"]
    asyncio.run(g.chat([{"role": "user", "content": "third"}]))
    assert hits["xai"] == before  # breaker open: primary skipped entirely
    assert any(u.status()["state"] == "open" for u in g.upstreams if u.provider == "xai")


def test_all_fail_raises(monkeypatch):
    g = mk({"AVRONE_PROVIDER": "xai", "AVRONE_LLM_API_KEY": "k", "AVRONE_LLM_RETRIES": "1", "AVRONE_LLM_BACKOFF": "0"},
           monkeypatch, lambda req: httpx.Response(503, text="down"))
    with pytest.raises(gateway.LLMError):
        asyncio.run(g.chat([{"role": "user", "content": "hi"}]))


def test_lead_model_per_upstream(monkeypatch):
    seen = {}

    def handler(req):
        import json
        seen["model"] = json.loads(req.content)["model"]
        return reply("ok")

    g = mk({"AVRONE_PROVIDER": "xai", "AVRONE_LLM_API_KEY": "k", "AVRONE_LLM_MODEL": "grok-worker",
            "AVRONE_LEAD_MODEL": "grok-lead"}, monkeypatch, handler)
    asyncio.run(g.chat([{"role": "user", "content": "hi"}], lead=True))
    assert seen["model"] == "grok-lead"
    asyncio.run(g.chat([{"role": "user", "content": "hi"}]))
    assert seen["model"] == "grok-worker"



def test_ollama_json_mode_is_opt_in_and_provider_scoped(monkeypatch):
    seen = []

    def handler(req):
        import json
        seen.append({"url": str(req.url), "body": json.loads(req.content)})
        return reply('{"tool":"shell","args":{"cmd":"echo json"}}')

    g = mk({"AVRONE_PROVIDER": "ollama", "AVRONE_LLM_MODEL": "mimo-v2.6:9b"}, monkeypatch, handler)
    asyncio.run(g.chat([{"role": "system", "content": "Reply with JSON."},
                        {"role": "user", "content": "Call shell."}], json_mode=True))
    assert seen[-1]["body"]["response_format"] == {"type": "json_object"}
    asyncio.run(g.chat([{"role": "user", "content": "Say hello."}], json_mode=False))
    assert "response_format" not in seen[-1]["body"]


def test_ollama_json_mode_only_applies_after_provider_failover(monkeypatch):
    seen = []

    def handler(req):
        import json
        body = json.loads(req.content)
        seen.append((str(req.url), body))
        if "x.ai" in str(req.url):
            return httpx.Response(429, text="rate limited")
        return reply('{"final":"ok"}')

    g = mk({
        "AVRONE_PROVIDER": "xai",
        "AVRONE_LLM_API_KEY": "k",
        "AVRONE_LLM_FALLBACKS": "ollama:mimo-v2.6:9b",
        "AVRONE_LLM_RETRIES": "1",
        "AVRONE_LLM_BACKOFF": "0",
    }, monkeypatch, handler)
    out = asyncio.run(g.chat([{"role": "system", "content": "JSON only."},
                              {"role": "user", "content": "Respond with JSON."}], json_mode=True))
    assert out == '{"final":"ok"}'
    assert "response_format" not in seen[0][1]
    assert seen[1][1]["response_format"] == {"type": "json_object"}
