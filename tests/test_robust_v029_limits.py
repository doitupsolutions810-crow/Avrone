"""v0.29: /api/chat is bounded. Before: any number of background runs could be started at once (each
holds an asyncio task, run state, agent bookkeeping and a run log), and a message of any size was
copied into every agent prompt. Now: 429 past AVRONE_MAX_BACKGROUND_RUNS in flight, 413 past
AVRONE_MAX_MESSAGE_CHARS."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from avrone import server


class SlowCore:
    def __init__(self):
        self.gate = asyncio.Event()

    async def chat(self, msg, run_id=None):
        await self.gate.wait()
        return {"answer": "ok", "run_id": run_id}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    with TestClient(server.app) as c:
        fake = SlowCore()
        monkeypatch.setitem(server.state, "core", fake)
        monkeypatch.setattr(server, "core", lambda: fake)
        yield c, fake


def test_background_cap(client, monkeypatch):
    c, fake = client
    monkeypatch.setenv("AVRONE_MAX_BACKGROUND_RUNS", "2")
    ok = [c.post("/api/chat", json={"message": "x", "mode": "chat", "background": True}) for _ in range(2)]
    assert [r.status_code for r in ok] == [200, 200]
    r = c.post("/api/chat", json={"message": "x", "mode": "chat", "background": True})
    assert r.status_code == 429 and "AVRONE_MAX_BACKGROUND_RUNS" in r.json()["detail"]
    server.state["tasks"].clear()  # runs finished -> room again
    assert c.post("/api/chat", json={"message": "x", "mode": "chat", "background": True}).status_code == 200


def test_message_cap(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", "100")
    r = c.post("/api/chat", json={"message": "y" * 101, "mode": "chat", "background": True})
    assert r.status_code == 413 and "AVRONE_MAX_MESSAGE_CHARS" in r.json()["detail"]
    r = c.post("/api/chat", json={"message": "q", "mode": "deep", "first_answer": "z" * 101})
    assert r.status_code == 413
    assert c.post("/api/chat", json={"message": "y" * 100, "mode": "chat", "background": True}).status_code == 200


@pytest.mark.parametrize("raw,expect", [(None, 32), ("", 32), ("junk", 32), ("0", 1), ("-5", 1), ("8", 8),
                                        ("999999", server.HARD_MAX_BACKGROUND_RUNS)])
def test_cap_parsing(monkeypatch, raw, expect):
    if raw is None:
        monkeypatch.delenv("AVRONE_MAX_BACKGROUND_RUNS", raising=False)
    else:
        monkeypatch.setenv("AVRONE_MAX_BACKGROUND_RUNS", raw)
    assert server.max_background_runs() == expect


def test_message_cap_hard_limit(monkeypatch):
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", str(10 ** 12))
    assert server.max_message_chars() == server.HARD_MAX_MESSAGE_CHARS
    monkeypatch.delenv("AVRONE_MAX_MESSAGE_CHARS")
    assert server.max_message_chars() == server.DEFAULT_MAX_MESSAGE_CHARS
