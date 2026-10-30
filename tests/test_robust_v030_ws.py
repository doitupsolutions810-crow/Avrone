"""v0.30: /ws/events frees a subscriber the moment its client leaves. Before: the handler only noticed
on the next failed send, so on a quiet node every dropped dashboard kept a 1000-slot queue (and the
handler) alive indefinitely, and a send error other than WebSocketDisconnect escaped as a server error."""
import time

import pytest
from fastapi.testclient import TestClient

from avrone import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    with TestClient(server.app) as c:
        yield c


def _wait(pred, s=3.0):
    end = time.time() + s
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_idle_disconnect_unsubscribes(client):
    codex = server.core().codex
    base = len(codex._subscribers)
    for _ in range(5):
        with client.websocket_connect("/ws/events"):
            assert _wait(lambda: len(codex._subscribers) > base)
    # no event was ever emitted, yet every subscriber is gone
    assert _wait(lambda: len(codex._subscribers) == base)


def test_events_still_stream(client):
    codex = server.core().codex
    with client.websocket_connect("/ws/events") as ws:
        assert _wait(lambda: len(codex._subscribers) >= 1)
        codex.emit("r-ws", "run.start", "Lead", task="t")
        ev = ws.receive_json()
        assert ev["run_id"] == "r-ws" and ev["kind"] == "run.start"


@pytest.mark.integration
def test_real_uvicorn_idle_disconnect_frees_subscriber(tmp_path, monkeypatch):
    """Under a real server the app task is not cancelled when the client drops, which is where the
    old handler leaked: it sat in q.get() until the next event."""
    import base64
    import os
    import socket
    import threading

    import uvicorn
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error", ws="auto"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: srv.started, 10)
        codex = server.core().codex
        base = len(codex._subscribers)
        for _ in range(3):
            c = socket.create_connection(("127.0.0.1", port))
            key = base64.b64encode(os.urandom(16)).decode()
            c.sendall((f"GET /ws/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            assert b"101" in c.recv(1024).split(b"\r\n")[0]
            assert _wait(lambda: len(codex._subscribers) > base)
            c.close()  # vanish without a close frame, no event ever emitted
        assert _wait(lambda: len(codex._subscribers) == base, 5)
    finally:
        srv.should_exit = True
        th.join(10)
