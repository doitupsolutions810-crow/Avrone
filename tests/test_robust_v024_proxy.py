"""v0.24 robustness: a local model server (the Ollama default) must be reached directly even when
the environment sets HTTP(S)_PROXY; remote providers keep honouring the proxy."""
import asyncio
import types

import httpx
import pytest

from avrone import gateway
from avrone import llm as llm_mod
from avrone.config import Settings


@pytest.mark.parametrize("url,want", [
    ("http://127.0.0.1:11434/v1", True), ("http://localhost:11434/v1", True), ("http://[::1]:8080/v1", True),
    ("http://127.8.9.1/v1", True), ("http://api.localhost/v1", True),
    ("https://api.x.ai/v1", False), ("http://10.0.0.5:11434/v1", False), ("http://host.docker.internal:11434", False),
    ("", False), ("http://[bad/v1", False),
])
def test_is_loopback_url(url, want):
    assert gateway.is_loopback_url(url) is want


class Recorder:
    seen: list = []

    def __init__(self, **kw):
        Recorder.seen.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                              request=httpx.Request("POST", url))


@pytest.fixture()
def rec(monkeypatch):
    Recorder.seen = []
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(gateway, "HTTP_TRANSPORT", None)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", Recorder)
    return Recorder


def _gw(monkeypatch, provider, url):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    g = gateway.Gateway(Settings())
    g.upstreams = [gateway.Upstream(provider, url, "m", "")]
    return g


def test_gateway_local_bypasses_proxy(monkeypatch, rec):
    g = _gw(monkeypatch, "ollama", "http://127.0.0.1:11434/v1")
    assert asyncio.run(g.chat([{"role": "user", "content": "hi"}])) == "ok"
    assert rec.seen[-1].get("trust_env") is False


def test_gateway_remote_keeps_proxy(monkeypatch, rec):
    g = _gw(monkeypatch, "xai", "https://api.x.ai/v1")
    asyncio.run(g.chat([{"role": "user", "content": "hi"}]))
    assert "trust_env" not in rec.seen[-1]


@pytest.mark.integration
def test_real_client_reaches_loopback_despite_bogus_proxy(monkeypatch):
    """Without the fix httpx sends 127.0.0.1 through the env proxy (ConnectError on port 9)."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    seen = []

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        seen.append(True)
        body = b'{"choices":[{"message":{"content":"direct"}}]}'
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    async def main():
        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(gateway, "HTTP_TRANSPORT", None)
        g = _gw(monkeypatch, "ollama", f"http://127.0.0.1:{port}/v1")
        try:
            return await g._request(g.upstreams[0], [{"role": "user", "content": "x"}], None, 0.1, None)
        finally:
            srv.close()
    assert asyncio.run(main()) == "direct" and seen
