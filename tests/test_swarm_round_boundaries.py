"""Adversarial projection and signed peer transport regressions for a swarm round."""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from avrone import bridge as bridge_mod
from avrone import fleet as fleet_mod
from avrone import server
from avrone.bridge import Bridge, blackboard_posts, normalize_blackboard_post, public_post
from avrone.fleet import FleetManager, sign


@pytest.fixture()
def peer(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "swarm-round-fixture")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    core = MagicMock()
    core.active = {}
    core.tools.names.return_value = []
    core.runner.kind = "local"
    return FleetManager(core, tmp_path)


def strict_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize("bad_ts", [float("nan"), float("inf"), -float("inf"), 10**1000, True],
                         ids=["nan", "infinity", "negative-infinity", "huge-int", "bool"])
def test_historical_blackboard_timestamp_and_nested_extras_are_json_safe(tmp_path, bad_ts):
    folder = tmp_path / "bridge"
    folder.mkdir()
    record = {
        "id": "historical", "run_id": "disk", "agent": {"label": "scout"},
        "role": 7, "kind": "finding", "text": "kept", "refs": [],
        "ts": bad_ts, "origin_node": "local",
        "extra": {"key\ud800": ["value\ud800", float("nan"), {"n": float("inf")}]},
    }
    path = folder / "disk.jsonl"
    path.write_text(json.dumps(record) + "\n")
    original = path.read_bytes()
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    first = blackboard_posts(core)
    second = blackboard_posts(core)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    strict_json(second)
    assert len(first) == 1 and first[0]["id"] == "historical"
    assert math.isfinite(first[0]["ts"]) and first[0]["ts"] > 0
    assert isinstance(first[0]["agent"], str) and first[0]["role"] == "7"
    assert first[0]["extra"] == {"key\ufffd": ["value\ufffd", 0.0, {"n": 0.0}]}
    strict_json(first)
    assert path.read_bytes() == original


def test_public_projection_bounds_cycles_depth_and_node_count_without_mutation():
    cyclic = []
    cyclic.append(cyclic)
    deep = "end"
    for _ in range(2500):
        deep = [deep]
    source = {"extra": list(range(20_000)), "deep": deep, "cycle": cyclic,
              "id": "keep-id", "kind": "finding", "text": "keep"}
    result = public_post(source)
    assert result["id"] == "keep-id" and result["kind"] == "finding"
    assert len(result["extra"]) <= 2_000
    strict_json(result)
    assert len(source["extra"]) == 20_000 and source["cycle"][0] is cyclic
    # Each boundary also works on its own, before any sibling consumes the budget.
    assert public_post({"cycle": cyclic}) == {"cycle": ["..."]}
    strict_json(public_post({"deep": deep}))


def test_projection_preserves_large_integer_precision_and_handles_unencodable_ints():
    exact = 10**1000 + 123
    result = public_post({"id": "large", "extra": [exact, 10**10_000]})
    assert result["extra"] == [exact, 0]
    assert json.loads(strict_json(result))["extra"][0] == exact


def test_timestamp_coercion_does_not_overflow_before_a_blackboard_write(monkeypatch):
    monkeypatch.setattr(bridge_mod.time, "time", lambda: 1234.0)
    record, error = normalize_blackboard_post({"id": "huge-clock", "text": "ok", "ts": 10**1000})
    assert error is None and record["ts"] == 1234.0
    assert bridge_mod._ts_key({"id": "huge-clock", "ts": 10**1000}) == (0.0, "huge-clock")


def test_replay_skips_over_digit_limit_corruption_and_keeps_following_record(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    good = {"id": "good-after", "kind": "finding", "text": "visible", "ts": 7}
    (folder / "disk.jsonl").write_text(
        '{"id":"bad","kind":"finding","text":"bad","ts":' + "9" * 5000 + "}\n" +
        json.dumps(good) + "\n")
    bridge = Bridge("disk", tmp_path)
    assert [row["id"] for row in bridge.posts()] == ["good-after"]


def test_http_blackboard_replays_poisoned_extras_and_overflow_timestamp(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    folder = tmp_path / "data" / "bridge"
    folder.mkdir(parents=True)
    row = {"id": "http-poison", "kind": "finding", "text": "retained",
           "ts": float("nan"), "extra": {"sur\ud800": [float("inf"), "x\ud800"]}}
    (folder / "disk.jsonl").write_text(json.dumps(row) + "\n")
    with TestClient(server.app) as client:
        for _ in range(2):
            response = client.get("/api/blackboard", headers={"X-Admin-Token": "test-token"})
            assert response.status_code == 200
            stored = response.json()["posts"][0]
            assert stored["id"] == "http-poison"
            strict_json(stored)
        payload = '{"id":"overflow-clock","text":"accepted","ts":' + "9" * 1000 + "}"
        posted = client.post("/api/blackboard", content=payload,
                             headers={"X-Admin-Token": "test-token"})
        assert posted.status_code == 200
        assert client.get("/api/blackboard", headers={"X-Admin-Token": "test-token"}).status_code == 200


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_peer_stream_stops_at_limit_and_closes_stream(peer, monkeypatch):
    monkeypatch.setattr(fleet_mod, "MAX_PEER_RESPONSE_BYTES", 16)
    stream = CountingStream([b"12345678"] * 100)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)))
    with pytest.raises(RuntimeError, match="byte limit"):
        await peer._post("http://child.test", "/api/fleet/run", {"prompt": "hi"})
    assert stream.reads == 3 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "identity, gzip"])
async def test_peer_rejects_unsolicited_compression_before_decoding(peer, monkeypatch, encoding):
    stream = CountingStream([b"not-even-compressed"])
    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="identity content encoding"):
        await peer._post("http://child.test", "/api/fleet/run", {})
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length", ["20000000", "NaN", "9" * 5000, "-1", "1, 1"])
async def test_invalid_or_excessive_peer_length_is_refused_before_read(peer, monkeypatch, length):
    stream = CountingStream([b"{}"])
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-length": length}, stream=stream)))
    with pytest.raises(RuntimeError, match="byte limit"):
        await peer._post("http://child.test", "/api/fleet/run", {})
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    b"[]", b"null", b'"answer"', b"{", b"\xff",
    '{"a":"ok"}'.encode("utf-16"), b'{"n":NaN}', b'{"n":Infinity}', b'{"n":1e999}',
    b'{"n":' + b"9" * 5000 + b"}",
    b"[" * 1500 + b"]" * 1500,
])
async def test_peer_response_must_be_strict_utf8_json_object(peer, monkeypatch, payload):
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)))
    with pytest.raises(RuntimeError, match="JSON object"):
        await peer._post("http://child.test", "/api/fleet/run", {})


@pytest.mark.asyncio
async def test_redirect_cannot_be_accepted_as_a_swarm_success(peer, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "http://elsewhere.test"},
                              json={"answer": "forged redirect success"})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="-> 307"):
        await peer._post("http://child.test", "/api/fleet/run", {})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_valid_streamed_reply_preserves_signed_request_and_integer_precision(peer, monkeypatch):
    requests = []
    exact = 10**1000 + 123
    data = json.dumps({"answer": "ok", "exact": exact}).encode()
    stream = CountingStream([data[:8], data[8:]])
    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    reply = await peer._post("http://child.test", "/api/fleet/run", {"prompt": "hi"})
    request = requests[0]
    expected = sign(peer.token, "POST", "/api/fleet/run", request.content,
                    request.headers["x-avrone-ts"], request.headers["x-avrone-nonce"])
    assert request.headers["x-avrone-sig"] == expected["x-avrone-sig"]
    assert reply == {"answer": "ok", "exact": exact} and stream.closed


@pytest.mark.asyncio
async def test_stalled_peer_reply_has_total_deadline_and_releases_stream(peer, monkeypatch):
    class Stalled(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'{"answer":"'
            await asyncio.Event().wait()
        async def aclose(self):
            self.closed = True
    stream = Stalled()
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)))
    with pytest.raises(TimeoutError):
        await peer._post("http://child.test", "/api/fleet/run", {}, timeout=0.03)
    assert stream.closed


@pytest.mark.asyncio
async def test_cancelled_swarm_dispatch_releases_load_and_stream(peer, monkeypatch):
    arrived = asyncio.Event()
    class Stalled(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            arrived.set()
            await asyncio.Event().wait()
            yield b"{}"
        async def aclose(self):
            self.closed = True
    stream = Stalled()
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)))
    peer.nodes["child"] = {"node_id": "child", "url": "http://child.test",
                           "last_seen": time.time(), "load": 0}
    task = asyncio.create_task(peer.dispatch(["child"], "chat", "hi", 0))
    await asyncio.wait_for(arrived.wait(), 1)
    assert peer.nodes["child"]["load"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.nodes["child"]["load"] == 0 and stream.closed


def test_real_loopback_peer_ignores_proxy_environment(peer, monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            calls.append((self.path, body))
            payload = b'{"answer":"direct"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "")
        result = asyncio.run(peer._post(
            f"http://127.0.0.1:{httpd.server_port}", "/api/fleet/run", {"prompt": "direct"}, timeout=2))
        assert result == {"answer": "direct"}
        assert calls == [("/api/fleet/run", b'{"prompt": "direct"}')]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
