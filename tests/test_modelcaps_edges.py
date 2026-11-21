"""Adversarial, offline tests for bounded Ollama model metadata discovery."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from avrone import modelcaps


def run(coro):
    return asyncio.run(coro)


def _payload_handler(show=None, tags=None, ps=None, version=None):
    payloads = {
        "/api/show": {"capabilities": ["completion", "tools"], "details": {"family": "qwen2"},
                      "model_info": {"qwen.context_length": 32768}, "parameters": "num_ctx 4096"}
        if show is None else show,
        "/api/tags": {"models": [{"name": "qwen2.5:3b", "digest": "abc", "size": 1234}]}
        if tags is None else tags,
        "/api/ps": {"models": [{"name": "qwen2.5:3b", "context_length": 4096,
                                "size": 1234, "size_vram": 456}]}
        if ps is None else ps,
        "/api/version": {"version": "0.13.0"} if version is None else version,
    }

    def handler(request):
        return httpx.Response(200, json=payloads[request.url.path])
    return httpx.MockTransport(handler)


def test_modelcaps_reads_native_metadata_and_preserves_unknowns():
    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b",
        transport=_payload_handler(),
    ))
    assert result["provider"] == "ollama"
    assert result["installed"] is True
    assert result["runtime"]["loaded"] is True
    assert result["capabilities"] == {
        "completion": True, "tools": True, "vision": False, "thinking": False,
    }
    assert result["context"] == {"model_info_tokens": 32768, "configured_num_ctx": 4096}
    assert result["ollama_version"] == "0.13.0"


@pytest.mark.parametrize("url", [
    "http://user:pass@127.0.0.1:11434/v1",
    "http://127.0.0.1:99999/v1",
    "http://127.0.0.1:11434/v1?route=/other",
    "http://127.0.0.1:11434/v1#fragment",
    "file:///tmp/ollama",
])
def test_modelcaps_rejects_ambiguous_or_unsafe_base_urls(url):
    with pytest.raises(modelcaps.OllamaCapabilityError):
        run(modelcaps.describe_ollama_model(url, "qwen2.5:3b", transport=_payload_handler()))


@pytest.mark.parametrize("model", ["", "../model", "space model", "x" * 129, 17, None])
def test_modelcaps_rejects_invalid_model_tags(model):
    with pytest.raises(modelcaps.OllamaCapabilityError, match="model tag is invalid"):
        run(modelcaps.describe_ollama_model("http://127.0.0.1:11434/v1", model,
                                            transport=_payload_handler()))


def test_modelcaps_rejects_oversized_show_response_from_content_length():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/show":
            return httpx.Response(200, content=b" " * (modelcaps.MAX_OLLAMA_METADATA_RESPONSE_BYTES + 1))
        return httpx.Response(200, json={})

    with pytest.raises(modelcaps.OllamaCapabilityError, match="metadata unavailable"):
        run(modelcaps.describe_ollama_model(
            "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
        ))
    assert seen == ["/api/show"]


def test_modelcaps_rejects_oversized_chunked_show_response_without_content_length():
    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b" " * (modelcaps.MAX_OLLAMA_METADATA_RESPONSE_BYTES + 1)

    def handler(request):
        return httpx.Response(200, stream=Oversized())

    with pytest.raises(modelcaps.OllamaCapabilityError, match="metadata unavailable"):
        run(modelcaps.describe_ollama_model(
            "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
        ))


def test_modelcaps_rejects_compressed_metadata_before_reading_body():
    class NeverRead(httpx.AsyncByteStream):
        def __init__(self):
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            yield b"{}"

    body = NeverRead()

    def handler(request):
        return httpx.Response(200, stream=body, headers={"content-encoding": "gzip"})

    with pytest.raises(modelcaps.OllamaCapabilityError, match="metadata unavailable"):
        run(modelcaps.describe_ollama_model(
            "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
        ))
    assert body.iterated is False


@pytest.mark.parametrize("raw", [
    b'{"details":{},"details":{}}',
    b'{"details":{"temperature":NaN}}',
    b'{"details":{"temperature":Infinity}}',
    b'{"details":',
    b"\xff",
])
def test_modelcaps_rejects_nonstandard_or_malformed_show_json(raw):
    def handler(request):
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    with pytest.raises(modelcaps.OllamaCapabilityError, match="metadata unavailable"):
        run(modelcaps.describe_ollama_model(
            "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
        ))


def test_modelcaps_accepts_exact_response_byte_limit():
    show = b'{"details":{}}'
    body = b" " * (modelcaps.MAX_OLLAMA_METADATA_RESPONSE_BYTES - len(show)) + show

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={})

    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
    ))
    assert result["details"]["family"] is None


def test_modelcaps_optional_inventory_failures_are_unknown_not_false():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, content=b'{"models":NaN}')
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"version": "0.13.0"})

    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
    ))
    assert result["installed"] is None
    assert result["runtime"]["loaded"] is False
    assert "installed model inventory unavailable" in result["issues"]


def test_model_context_walk_is_iterative_for_deep_metadata():
    value = {"context_length": 65536}
    for index in range(1500):
        value = {f"nested_{index}": value}
    found, truncated = modelcaps._context_lengths(value)
    assert found == [65536]
    assert truncated is False


def test_model_context_walk_reports_truncation(monkeypatch):
    monkeypatch.setattr(modelcaps, "MAX_MODEL_INFO_NODES", 2)
    value = {"context": {"context_length": 8192}, "x": {}, "y": {}}
    found, truncated = modelcaps._context_lengths(value)
    assert found == []
    assert truncated is True


@pytest.mark.parametrize("payload", [
    [],
    None,
    "not-an-object",
    {},
    {"models": {}},
    {"models": [None]},
    {"models": [1]},
    {"models": [{"name": {"nested": "bad"}}]},
])
def test_modelcaps_malformed_installed_inventory_stays_unknown(payload):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"version": "0.13.0"})

    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
    ))
    assert result["installed"] is None
    assert result["runtime"]["loaded"] is False
    assert "installed model inventory unavailable" in result["issues"]


@pytest.mark.parametrize("payload", [
    [],
    None,
    "not-an-object",
    {},
    {"models": {}},
    {"models": [False]},
    {"models": [{"model": ["bad", "name"]}]},
])
def test_modelcaps_malformed_running_inventory_stays_unknown(payload):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"version": "0.13.0"})

    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
    ))
    assert result["installed"] is False
    assert result["runtime"]["loaded"] is None
    assert "loaded model runtime inventory unavailable" in result["issues"]


def test_modelcaps_does_not_follow_metadata_redirects():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    with pytest.raises(modelcaps.OllamaCapabilityError, match="metadata unavailable"):
        run(modelcaps.describe_ollama_model(
            "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
        ))
    assert [request.url.host for request in requests] == ["127.0.0.1"]


def test_modelcaps_does_not_coerce_malformed_capability_objects_to_strings():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", {"tools": True}]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"version": 17})

    result = run(modelcaps.describe_ollama_model(
        "http://127.0.0.1:11434/v1", "qwen2.5:3b", transport=httpx.MockTransport(handler)
    ))
    assert result["capabilities"] == {
        "completion": None, "tools": None, "vision": None, "thinking": None,
    }
    assert result["ollama_version"] is None
    assert "model capability metadata malformed" in result["issues"]
    assert "Ollama version metadata malformed" in result["issues"]
