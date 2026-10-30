"""Round 16 coverage: the event stream's disconnect watcher survives a transport error, and a failed
fleet.json save cleans its temp file even when the cleanup itself fails."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone import server


def test_ws_receive_error_ends_stream_and_unsubscribes(monkeypatch):
    state = {}

    class FakeCodex:
        def subscribe(self):
            state["q"] = asyncio.Queue()
            return state["q"]

        def unsubscribe(self, q):
            state["unsub"] = q

    class FakeWS:
        headers, query_params, client = {}, {}, SimpleNamespace(host="127.0.0.1")

        async def accept(self):
            pass

        async def send_json(self, data):
            pass

        async def receive(self):
            raise RuntimeError("transport gone")
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(codex=FakeCodex()))
    asyncio.run(asyncio.wait_for(server.events(FakeWS()), 5))
    assert state["unsub"] is state["q"]


def test_ws_send_error_ends_stream(monkeypatch):
    state = {}

    class FakeCodex:
        def subscribe(self):
            q = asyncio.Queue()
            q.put_nowait(SimpleNamespace())
            state["q"] = q
            return q

        def unsubscribe(self, q):
            state["unsub"] = q

    class FakeWS:
        headers, query_params, client = {}, {}, SimpleNamespace(host="127.0.0.1")

        async def accept(self):
            pass

        async def send_json(self, data):
            raise OSError("broken pipe")

        async def receive(self):
            await asyncio.sleep(3600)
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(codex=FakeCodex()))
    monkeypatch.setattr(server, "asdict", lambda ev: {})
    asyncio.run(asyncio.wait_for(server.events(FakeWS()), 5))
    assert state["unsub"] is state["q"]


def test_fleet_save_cleanup_failure_still_raises_original(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    c = MagicMock()
    c.active = {}
    fm = fleet_mod.FleetManager(c, tmp_path)
    monkeypatch.setattr(fleet_mod.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("ro fs")))
    from pathlib import Path
    real_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    with pytest.raises(OSError, match="ro fs"):
        fm._save()
    monkeypatch.setattr(Path, "unlink", real_unlink)
