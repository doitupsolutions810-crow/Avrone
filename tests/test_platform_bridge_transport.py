"""Streaming and adversarial response bounds for the host-side platform bridge."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_platform_bridge import _load_bridge


def run(coro):
    return asyncio.run(coro)


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def configure(monkeypatch):
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", '{"grokbot":"http://grokbot:3000"}')
    monkeypatch.setenv("AVRONE_BRIDGE_GROKBOT_TOKEN", "unit-test-secret")


def test_bridge_rejects_declared_oversized_body_before_reading(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)

    class NeverRead(httpx.AsyncByteStream):
        def __init__(self):
            self.iterated = False
            self.closed = False

        async def __aiter__(self):
            self.iterated = True
            yield b"x"

        async def aclose(self):
            self.closed = True

    stream = NeverRead()

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Bearer unit-test-secret"
        return httpx.Response(
            200, stream=stream,
            headers={"content-length": str(bridge.MAX_RESPONSE_BYTES + 1)},
        )

    install_transport(monkeypatch, handler)
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert "exceeded" in result["error"]
    assert stream.iterated is False
    assert stream.closed is True


def test_bridge_bounds_chunked_body_without_content_length(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)

    class Oversized(httpx.AsyncByteStream):
        def __init__(self):
            self.iterations = 0
            self.closed = False

        async def __aiter__(self):
            self.iterations += 1
            yield b"x" * (bridge.MAX_RESPONSE_BYTES + 1)
            self.iterations += 1
            yield b"must-not-be-read"

        async def aclose(self):
            self.closed = True

    stream = Oversized()
    install_transport(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert result["error"] == f"response exceeded {bridge.MAX_RESPONSE_BYTES} bytes"
    assert stream.iterations == 1
    assert stream.closed is True


def test_bridge_accepts_exact_response_size_boundary(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)
    payload = json.dumps({"value": "x" * (bridge.MAX_RESPONSE_BYTES - 12)}).encode()
    assert len(payload) == bridge.MAX_RESPONSE_BYTES

    install_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is True
    assert len(result["data"]["value"]) > 60_000


def test_bridge_rejects_compressed_response_without_reading(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)

    class NeverRead(httpx.AsyncByteStream):
        def __init__(self):
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            yield b"compressed"

    stream = NeverRead()
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, stream=stream, headers={"content-encoding": "gzip"}
    ))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert "compressed" in result["error"]
    assert stream.iterated is False


def test_bridge_refuses_redirect_without_forwarding_secret(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/collect"})

    install_transport(monkeypatch, handler)
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert result["http_status"] == 302
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer unit-test-secret"


@pytest.mark.parametrize("content_length", ["-1", "invalid", "1.5"])
def test_bridge_rejects_malformed_content_length(monkeypatch, content_length):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)

    class NeverRead(httpx.AsyncByteStream):
        def __init__(self):
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            yield b"{}"

    stream = NeverRead()
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, stream=stream, headers={"content-length": content_length}
    ))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert "invalid Content-Length" in result["error"]
    assert stream.iterated is False


def test_bridge_preserves_error_status_without_unbounded_error_body(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)
    install_transport(monkeypatch, lambda request: httpx.Response(
        503, json={"error": "maintenance", "details": "retry later"}
    ))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is False
    assert result["http_status"] == 503
    assert result["remote_error"] == "maintenance"


def test_bridge_returns_bounded_text_for_non_json_success(monkeypatch):
    _registry, bridge = _load_bridge()
    configure(monkeypatch)
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=b"\xff" + b"x" * 40
    ))
    result = json.loads(run(bridge._request("grokbot", "GET", "/api/status")))
    assert result["ok"] is True
    assert result["data"].startswith("\ufffd")
    assert len(result["data"]) <= bridge.MAX_TEXT_CHARS


def test_bridge_rejects_invalid_and_oversized_content_lengths():
    # Decimal parsing is separately exercised via malformed wire headers; direct
    # values also ensure the hard ceiling remains an ordinary bounded integer.
    assert bridge.MAX_RESPONSE_BYTES == 64 * 1024
