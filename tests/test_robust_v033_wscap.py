"""v0.33: live event-stream subscribers are capped. Before: any allowed caller could open unlimited
/ws/events connections, each with a 1000-event queue that every emit fans out to."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from avrone import codex as codex_mod
from avrone import server
from avrone.codex import Codex, SubscribersFull


def test_cap_env(monkeypatch):
    assert codex_mod.max_subscribers() == 64
    for v, want in (("2", 2), ("0", 1), ("999999", 1024), ("x", 64)):
        monkeypatch.setenv("AVRONE_MAX_WS_SUBSCRIBERS", v)
        assert codex_mod.max_subscribers() == want


def test_subscribe_refuses_past_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_MAX_WS_SUBSCRIBERS", "2")
    cx = Codex(tmp_path)
    a, _ = cx.subscribe(), cx.subscribe()
    with pytest.raises(SubscribersFull, match="AVRONE_MAX_WS_SUBSCRIBERS"):
        cx.subscribe()
    cx.unsubscribe(a)
    cx.subscribe()  # a freed slot is usable again


def test_ws_closes_1013_when_full_and_frees_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_MAX_WS_SUBSCRIBERS", "1")
    with TestClient(server.app) as c:
        cx = server.core().codex
        base = len(cx._subscribers)
        monkeypatch.setenv("AVRONE_MAX_WS_SUBSCRIBERS", str(base + 1))
        with c.websocket_connect("/ws/events"):
            with pytest.raises(WebSocketDisconnect) as e:
                with c.websocket_connect("/ws/events") as ws2:
                    ws2.receive_json()
            assert e.value.code == 1013
        with c.websocket_connect("/ws/events"):
            pass


def test_accept_failure_releases_slot(tmp_path, monkeypatch):
    import asyncio

    class WS:
        headers, query_params, client = {}, {}, None

        async def accept(self):
            raise OSError("client vanished")
    monkeypatch.setattr(server, "caller_allowed", lambda *a: True)
    cx = Codex(tmp_path)
    monkeypatch.setattr(server, "core", lambda: type("C", (), {"codex": cx})())
    with pytest.raises(OSError):
        asyncio.run(server.events(WS()))
    assert not cx._subscribers
