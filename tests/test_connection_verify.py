from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.connection_verify import verify_connections


@pytest.fixture(autouse=True)
def live_settings(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "openai")
    monkeypatch.setenv("AVRONE_LLM_BASE_URL", "https://api.openai.example/v1")
    monkeypatch.setenv("AVRONE_LLM_MODEL", "model-test")
    monkeypatch.setenv("AVRONE_LLM_API_KEY", "test-secret-key-do-not-print")
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", "1")
    monkeypatch.setenv("AVRONE_LLM_BACKOFF", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    gateway.HTTP_TRANSPORT = None
    yield
    gateway.HTTP_TRANSPORT = None


def test_config_report_makes_no_network_request_and_never_discloses_secrets():
    settings = Settings()
    report = asyncio.run(verify_connections(settings))
    serialized = json.dumps(report)

    assert report["ok"] is True
    assert report["fully_verified"] is False
    assert report["scope"] == "configuration_only"
    assert report["live_probe"]["status"] == "not_requested"
    assert "test-secret-key-do-not-print" not in serialized
    assert "api.openai.example" not in serialized
    assert "model-test" not in serialized


def test_missing_cloud_key_fails_preflight_without_leaking_endpoint(monkeypatch):
    monkeypatch.delenv("AVRONE_LLM_API_KEY", raising=False)
    settings = Settings()
    report = asyncio.run(verify_connections(settings, live=True))

    assert report["ok"] is False
    assert report["live_probe"]["status"] == "blocked"
    assert any(c["id"] == "cloud_credential" and c["status"] == "fail" for c in report["checks"])
    serialized = json.dumps(report)
    assert "api.openai.example" not in serialized
    assert "test-secret-key-do-not-print" not in serialized


def test_invalid_fallback_provider_fails_preflight(monkeypatch):
    monkeypatch.setenv("AVRONE_LLM_FALLBACKS", "not-a-provider:model")
    report = asyncio.run(verify_connections(Settings()))

    assert report["ok"] is False
    assert any(c["id"] == "fallback_configuration" and c["status"] == "fail" for c in report["checks"])


def test_live_probe_uses_primary_only_and_discards_response_content(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret-key-do-not-print"
        body = json.loads(request.content)
        assert body["model"] == "model-test"
        assert body["temperature"] == 0
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setenv("AVRONE_LLM_FALLBACKS", "ollama:qwen2.5:3b")
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    report = asyncio.run(verify_connections(Settings(), live=True))
    serialized = json.dumps(report)

    assert report["ok"] is True
    assert report["fully_verified"] is True
    assert report["live_probe"]["status"] == "pass"
    assert report["live_probe"]["route"] == ["openai:ok"]
    assert report["live_probe"]["response_characters"] == 2
    assert len(seen) == 1  # fallback was not contacted
    assert "test-secret-key-do-not-print" not in serialized
    assert "api.openai.example" not in serialized
    assert '"OK"' not in serialized


def test_live_probe_reports_safe_auth_failure_without_response_body(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key test-secret-key-do-not-print")

    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    report = asyncio.run(verify_connections(Settings(), live=True))
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert report["live_probe"]["status"] == "failed"
    assert report["live_probe"]["error_category"] == "authentication_failed"
    assert "test-secret-key-do-not-print" not in serialized
    assert "invalid key" not in serialized


def test_live_probe_is_blocked_in_mock_mode(monkeypatch):
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    report = asyncio.run(verify_connections(Settings(), live=True))

    assert report["ok"] is False
    assert report["live_probe"] == {"status": "blocked", "reason": "mock_mode"}
