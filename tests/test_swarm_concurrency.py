"""Exercise the actual ASGI boundary while synchronous Swarm I/O is stalled."""
import concurrent.futures
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from avrone.swarm_api import install_routes
from tests.test_swarm_api import FakeClient, body, setup


def test_slow_swarm_enqueue_does_not_block_unrelated_health_requests(tmp_path):
    app, client = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = client.send_request

    def blocked(request):
        entered.set()
        assert release.wait(8), "test failed to release stalled network call"
        return original(request)

    client.send_request = blocked

    @app.get("/health-probe")
    async def health():
        return {"responsive": True}

    with TestClient(app) as http, concurrent.futures.ThreadPoolExecutor(2) as pool:
        pending = pool.submit(http.post, "/api/swarm/enqueue", json=body(),
                              headers={"X-Admin-Token": "operator-token"})
        try:
            assert entered.wait(3)
            probe = pool.submit(http.get, "/health-probe")
            assert probe.result(timeout=2).json() == {"responsive": True}
        finally:
            release.set()
            assert pending.result(timeout=5).status_code == 202


def test_concurrent_first_requests_open_one_dispatcher_and_close_it_once(tmp_path):
    app = FastAPI()
    created, closed = [], []

    def factory():
        client = FakeClient()
        client._http = SimpleNamespace(close=lambda: closed.append(client))
        created.append(client)
        return client

    install_routes(app, lambda: None, lambda request, token: None,
                   SimpleNamespace(data_dir=tmp_path), client_factory=factory)
    with TestClient(app) as http, concurrent.futures.ThreadPoolExecutor(8) as pool:
        barrier = threading.Barrier(8)

        def send(index):
            barrier.wait(timeout=5)
            return http.post("/api/swarm/enqueue", json=body(message_id=f"m-{index}"))

        responses = list(pool.map(send, range(8)))
        assert all(response.status_code == 202 for response in responses)
        assert len(created) == 1
    assert closed == created


def test_parallel_duplicate_retry_sends_once_and_releases_message_locks(tmp_path):
    app, client = setup(tmp_path)
    with TestClient(app) as http, concurrent.futures.ThreadPoolExecutor(8) as pool:
        headers = {"X-Admin-Token": "operator-token"}
        barrier = threading.Barrier(8)

        def send(_index):
            barrier.wait(timeout=5)
            return http.post("/api/swarm/enqueue", json=body(), headers=headers)

        responses = list(pool.map(send, range(8)))
        assert all(response.status_code == 202 for response in responses)
        assert all(response.json()["state"] == "queued" for response in responses)
        assert len(client.calls) == 1
        assert not app.state.swarm_dispatcher._flights


def test_corrupt_swarm_status_returns_integrity_conflict(tmp_path):
    app, _ = setup(tmp_path)
    with TestClient(app) as http:
        headers = {"X-Admin-Token": "operator-token"}
        assert http.post("/api/swarm/enqueue", json=body(), headers=headers).status_code == 202
        box = app.state.swarm_dispatcher.outbox
        box._db.execute("UPDATE swarm_outbox SET request_sha256='corrupt'")
        result = http.get("/api/swarm/requests/msg-1", headers=headers)
        assert result.status_code == 409
        assert "integrity" in result.json()["detail"]


@pytest.mark.parametrize("policy", ["all", "loopback", "admin"])
@pytest.mark.parametrize("path,method", [("/api/swarm/enqueue", "post"),
    ("/api/swarm/retry/missing", "post"), ("/api/swarm/requests/missing", "get")])
def test_real_server_swarm_routes_require_operator_even_if_public_chat_is_open(
    tmp_path, monkeypatch, policy, path, method
):
    from avrone import server

    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", policy)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    with TestClient(server.app, client=("127.0.0.1", 50000)) as http:
        kwargs = {"json": body()} if path.endswith("enqueue") else {}
        assert getattr(http, method)(path, **kwargs).status_code in (401, 403)
        assert getattr(http, method)(path, headers={"X-Admin-Token": "wrong"}, **kwargs).status_code in (401, 403)
        assert getattr(server.app.state, "swarm_dispatcher", None) is None
