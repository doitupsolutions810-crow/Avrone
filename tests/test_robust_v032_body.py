"""v0.32: request bodies are capped before they are buffered. Before: every route read the whole body
first (fleet HMAC needs it; /api/chat's model parses it), so an unauthenticated caller could make the
server hold any amount of memory, e.g. a 1 GB POST to /api/fleet/register answered 401 only after
the gigabyte was in RAM."""
import pytest
from fastapi.testclient import TestClient

from avrone import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", "1")
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", "1000")
    with TestClient(server.app) as c:
        yield c


def test_limits_derive_from_message_cap_and_quota(monkeypatch):
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", "1000")
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", "1")
    assert server.max_body_bytes("/api/chat") == 12 * 1000 + 64 * 1024
    from avrone import agentpack
    assert server.max_body_bytes("/api/fleet/agentpack") >= agentpack.quota_bytes() * 4 // 3
    monkeypatch.delenv("AVRONE_MAX_MESSAGE_CHARS")
    monkeypatch.delenv("AVRONE_MAX_BODY_BYTES")
    assert server.max_body_bytes() == server.DEFAULT_MAX_BODY_BYTES
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", str(10 ** 12))
    assert server.max_body_bytes() == server.HARD_MAX_BODY_BYTES
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", "junk")
    assert server.max_body_bytes() == server.DEFAULT_MAX_BODY_BYTES


def test_declared_oversize_refused_413(client):
    lim = server.max_body_bytes("/api/fleet/register")
    r = client.post("/api/fleet/register", content=b"x" * (lim + 1))
    assert r.status_code == 413 and "AVRONE_MAX_BODY_BYTES" in r.json()["detail"]


def test_chunked_oversize_refused_413(client):
    lim = server.max_body_bytes("/api/fleet/register")

    def gen():
        for _ in range(lim // 8192 + 2):
            yield b"y" * 8192
    r = client.post("/api/fleet/register", content=gen())
    assert r.status_code == 413


def test_under_cap_reaches_route(client):
    r = client.post("/api/fleet/register", content=b"{}")
    assert r.status_code == 401  # auth runs as before
    r = client.post("/api/chat", json={"message": "x" * 1001})
    assert r.status_code == 413 and "AVRONE_MAX_MESSAGE_CHARS" in r.json()["detail"]  # char cap still owns this


def test_get_and_ws_unaffected(client):
    assert client.get("/api/status").status_code == 200
    with client.websocket_connect("/ws/events"):
        pass


def test_bad_content_length_header_falls_back_to_counting(client):
    import asyncio
    sent = []

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    mw = server.BodyLimit(app)
    msgs = iter([{"type": "http.request", "body": b"abc", "more_body": False}])

    async def receive():
        return next(msgs)

    async def send(m):
        sent.append(m)
    asyncio.run(mw({"type": "http", "path": "/x", "headers": [(b"content-length", b"zz")]}, receive, send))
    assert sent[0]["status"] == 200


def test_oversize_after_response_started_is_not_double_sent():
    import asyncio
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
    mw = server.BodyLimit(app)

    async def receive():
        return {"type": "http.request", "body": b"z" * (server.max_body_bytes() + 1)}

    async def send(m):
        sent.append(m)
    asyncio.run(mw({"type": "http", "path": "/x", "headers": []}, receive, send))
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [200]
