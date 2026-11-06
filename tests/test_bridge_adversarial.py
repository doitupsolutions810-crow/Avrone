"""Malformed peers, broken storage and concurrent callers must never manufacture a pass."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from avrone import grokbot, integration
from tests.test_grokbot_integration import Core, response


@pytest.fixture
def peer(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000")
    monkeypatch.setenv("GROK_BRIDGE_TOKEN", "test-token")
    return monkeypatch


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"value":1e999}', b'{"ok":true,"ok":false}',
    b'{"nested":{"key":1,"key":2}}', b'{"text":"\\ud800"}', b'{"text":"\xff"}',
    '{"ok":true}'.encode("utf-16"), b'{"value":NaN}', b'{"value":Infinity}',
    b'{"nested":' + b'[' * 70 + b'0' + b']' * 70 + b'}'])
async def test_peer_json_rejects_ambiguous_nonfinite_and_invalid_unicode(peer, raw):
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))
    with pytest.raises(ValueError):
        await grokbot.request_json("http://grok-bot:3000", "/api/status")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["http://grok-bot:\n3000", "http://grok-bot\t:3000",
    "http://grok-bot:3000?", "http://grok-bot:3000#", "http://grok-bot\\evil"])
async def test_origin_controls_are_rejected_before_io(peer, target):
    calls = []
    peer.setenv("AVRONE_GROKBOT_URL", target)
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, json=response())))
    assert not (await grokbot.ask("Check", correlation_id="council-test"))["ok"]
    assert not calls


@pytest.mark.asyncio
async def test_stream_failure_does_not_echo_private_details(peer):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":'
            raise ValueError("private-transport-token")
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, stream=Broken())))
    result = await grokbot.ask("Check")
    assert not result["ok"] and "private-transport-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_full_returned_answer_remains_bound_to_its_receipt(peer):
    body = response(answer="x" * 13000)
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    result = await grokbot.ask("Check", correlation_id="council-test")
    assert result["ok"]
    assert hashlib.sha256(result["answer"].encode()).hexdigest() == result["receipt"]["answer_sha256"]


@pytest.mark.asyncio
async def test_total_deadline_includes_body_reading(peer):
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.1)
            yield b'{}'
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow())))
    with pytest.raises(TimeoutError):
        await grokbot.request_json("http://grok-bot:3000", "/api/status", timeout=.01)


@pytest.mark.asyncio
async def test_compressed_response_is_refused_before_body_read(peer):
    class Unread(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("encoded body must not be read")
            yield b''
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-encoding":"gzip"}, stream=Unread())))
    with pytest.raises(ValueError):
        await grokbot.request_json("http://grok-bot:3000", "/api/status")


@pytest.mark.parametrize("agents", [1, True, "report", {}, [None], [{"result": None}],
    [{"result": "  ERROR: backend failed"}], [{"result": "\n[Worker failed: unavailable]"}],
    [{"result":"[Worker\nOne failed: backend unavailable]"}], [{"result":"[Worker [One] failed: backend unavailable]"}]])
def test_malformed_worker_shapes_never_raise_or_promote(agents):
    assert not integration._complete({"answer":"Polished Lead answer", "agents":agents})


@pytest.mark.parametrize("metadata", [{"ok":False},{"ok":None},{"ok":0},{"state":"HOLD"},
    {"status":"running"},{"status":"failed"},{"status":{}},{"mode":"chat"}])
def test_explicit_failure_metadata_overrides_a_polished_answer(metadata):
    assert not integration._complete({"answer":"Answer", "agents":[{"result":"Report"}], **metadata})


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale_id", "missing_worker", "short_squad", "nonfinite", "oversized"])
async def test_bad_local_evidence_is_held_without_calling_lead(tmp_path, monkeypatch, fault):
    class BadCore(Core):
        lead_calls = 0
        async def squad(self, message, n, rid):
            out = await super().squad(message, n, rid)
            if fault == "stale_id": out["run_id"] = "old-run"
            if fault == "missing_worker": out["agents"] = []
            if fault == "short_squad": out["agents"] = out["agents"][:1]
            if fault == "nonfinite": out["metric"] = float("inf")
            if fault == "oversized": out["extra"] = "x" * (3 * 1024 * 1024)
            return out
        async def _chat(self, *args, **kwargs):
            self.lead_calls += 1
            raise AssertionError("unusable evidence must not reach the Lead")
    async def ask(_, correlation_id): return response(correlation_id=correlation_id)
    monkeypatch.setattr(grokbot, "ask", ask)
    core = BadCore(tmp_path)
    result = await integration.conclude(core, "Check", 2)
    assert core.lead_calls == 0
    assert result["state"] == "HOLD"
    assert len(json.dumps(result)) < 2 * 1024 * 1024


def app_for(root):
    app = FastAPI()
    integration.install_routes(app, lambda: None, lambda request, token: None, SimpleNamespace(data_dir=root))
    return app


@pytest.mark.parametrize("raw", ['[]','null','{"state":"COMPLETE"}',
    '{"value":1e999,"receipt_sha256":"x"}', '{"state":"HOLD","state":"COMPLETE","receipt_sha256":"x"}'])
def test_corrupt_receipts_are_integrity_errors(tmp_path, raw):
    folder = tmp_path / "integration"; folder.mkdir()
    rid = "council-" + "a" * 32
    (folder / (rid + ".json")).write_text(raw)
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/api/integration/receipts/" + rid).status_code == 409


def test_receipt_from_another_run_cannot_be_relabelled(tmp_path):
    folder = tmp_path / "integration"; folder.mkdir()
    rid = "council-" + "a" * 32
    body = {"run_id":"council-" + "b" * 32, "state":"COMPLETE"}
    body["receipt_sha256"] = hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
    (folder / (rid + ".json")).write_text(json.dumps(body))
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/api/integration/receipts/" + rid).status_code == 409


def test_storage_rejects_symlink_directory_without_reading_or_writing_outside(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (root / "integration").symlink_to(outside, target_is_directory=True)
    rid = "council-" + "a" * 32
    with pytest.raises(OSError):
        integration._store_receipt(SimpleNamespace(data_dir=root), rid, {"run_id":rid})
    assert not list(outside.iterdir())
    with TestClient(app_for(root)) as client:
        assert client.get("/api/integration/receipts/" + rid).status_code in (404,409)


def test_storage_never_overwrites_an_existing_receipt(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    rid = "council-" + "a" * 32
    integration._store_receipt(settings, rid, {"run_id":rid,"state":"HOLD"})
    stored = tmp_path / "integration" / (rid + ".json")
    before = stored.read_bytes()
    assert stored.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        integration._store_receipt(settings, rid, {"run_id":rid,"state":"COMPLETE"})
    assert stored.read_bytes() == before
    assert sorted(p.name for p in stored.parent.iterdir()) == [rid + ".json"]


def test_failed_sync_cleans_only_its_own_temporary_files(tmp_path, monkeypatch):
    def fail(_): raise OSError("private-storage-path")
    monkeypatch.setattr(integration.os, "fsync", fail)
    with pytest.raises(OSError):
        integration._store_receipt(SimpleNamespace(data_dir=tmp_path), "council-" + "a" * 32, {"state":"HOLD"})
    assert not list((tmp_path / "integration").iterdir())


def test_preexisting_temp_symlink_is_not_deleted_or_followed(tmp_path, monkeypatch):
    folder = tmp_path / "integration";folder.mkdir()
    rid = "council-" + "a" * 32
    fixed = "b" * 32
    outside = tmp_path / "outside";outside.write_text("keep")
    temp = folder / f".{rid}-{fixed}.tmp";temp.symlink_to(outside)
    monkeypatch.setattr(integration.uuid, "uuid4", lambda: SimpleNamespace(hex=fixed))
    with pytest.raises(FileExistsError):
        integration._store_receipt(SimpleNamespace(data_dir=tmp_path), rid, {"state":"HOLD"})
    assert temp.is_symlink() and outside.read_text() == "keep"
    assert not (folder / f".{rid}.lock").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_receipt_reader_refuses_nonregular_entries(tmp_path, kind):
    import os
    folder = tmp_path / "integration";folder.mkdir()
    rid = "council-" + "a" * 32
    path = folder / (rid + ".json")
    if kind == "symlink": path.symlink_to(tmp_path / "outside")
    elif kind == "directory": path.mkdir()
    else: os.mkfifo(path)
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/api/integration/receipts/" + rid).status_code in (404,409)


@pytest.mark.asyncio
async def test_storage_failure_is_sanitized_and_frees_council_slot(tmp_path, monkeypatch):
    async def broken(*args): raise OSError("private-mount-token")
    monkeypatch.setattr(integration, "conclude", broken)
    app = app_for(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        for _ in range(4):
            r = await client.post("/api/integration/conclude",json={"message":"Check"})
            assert r.status_code == 503 and "private-mount-token" not in r.text
            assert not app.state.councils


@pytest.mark.asyncio
async def test_diagnostics_do_not_claim_inference_or_forward_credentials_to_redirects(peer):
    real = httpx.AsyncClient
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.host == "provider":
            return httpx.Response(302,headers={"location":"http://other-host/steal"})
        return httpx.Response(200,json={"ready":True,"tools":["browser"]})
    peer.setattr(httpx,"AsyncClient",lambda **kw: real(**{**kw,"transport":httpx.MockTransport(handler)}))
    settings = SimpleNamespace(llm_mode="live",llm_base_url="http://provider/v1",llm_api_key="private-key")
    result = await integration.connection_status(settings)
    assert result["state"] == "DEGRADED" and not result["live_council_verified"]
    assert not result["model"]["inference_verified"]
    assert {r.url.host for r in seen} == {"provider","grok-bot"}
    assert next(r for r in seen if r.url.host == "provider").headers["authorization"] == "Bearer private-key"
    assert "authorization" not in next(r for r in seen if r.url.host == "grok-bot").headers


def test_seeded_json_and_component_mutations_remain_bounded():
    import random
    rng = random.Random(0xA12)
    atoms = [None, True, False, 0, -1, 1e-200, "text", "🚀 café", "quotes\"\\\n"]
    for i in range(1500):
        original = {"id":i,"items":[rng.choice(atoms) for _ in range(rng.randrange(12))]}
        raw = json.dumps(original,ensure_ascii=False).encode()
        assert grokbot.strict_json(raw) == original
        changed = bytearray(raw)
        changed[rng.randrange(len(changed))] = rng.randrange(256)
        try:
            out = grokbot.strict_json(bytes(changed))
            json.dumps(out,ensure_ascii=False,allow_nan=False).encode()
        except grokbot.ProtocolError:
            pass
        bad = {"answer":rng.choice(atoms),"agents":rng.choice(atoms + [[None],{},[{"result":" ERROR: unavailable"}]])}
        assert integration._complete(bad) is False
        snapshot = integration._snapshot({**original,"unexpected":rng.choice([float("inf"),float("nan"),{1},b"raw",None])})
        assert len(json.dumps(snapshot,allow_nan=False).encode()) < integration.MAX_COMPONENT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, "1", 10**1000])
async def test_invalid_timeout_budgets_fail_before_io(peer, value):
    with pytest.raises(ValueError):
        await grokbot.request_json("http://grok-bot:3000", "/api/status", timeout=value)


@pytest.mark.parametrize("rid", [None, "../outside", "/tmp/outside", "council-abc", "council-"+"A"*32])
def test_storage_id_rejected_before_touching_disk(tmp_path, rid):
    with pytest.raises(ValueError):
        integration._store_receipt(SimpleNamespace(data_dir=tmp_path), rid, {})
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_real_council_cancellation_stops_both_pending_components(tmp_path, monkeypatch):
    entered, cancelled = set(), set()
    both = asyncio.Event()
    async def blocked(name):
        entered.add(name)
        if len(entered) == 2: both.set()
        try: await asyncio.Event().wait()
        finally: cancelled.add(name)
    core = Core(tmp_path)
    monkeypatch.setattr(core,"squad",lambda *args: blocked("local"))
    monkeypatch.setattr(grokbot,"ask",lambda *args,**kw: blocked("peer"))
    task = asyncio.create_task(integration.conclude(core,"Check",2))
    await asyncio.wait_for(both.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert cancelled == {"local","peer"}
    assert not (tmp_path/"integration").exists()


@pytest.mark.asyncio
async def test_lead_cancellation_cannot_publish_a_complete_receipt(tmp_path, monkeypatch):
    entered = asyncio.Event()
    class WaitingLead(Core):
        async def _chat(self,*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
    async def ask(_,correlation_id): return response(correlation_id=correlation_id)
    monkeypatch.setattr(grokbot,"ask",ask)
    task = asyncio.create_task(integration.conclude(WaitingLead(tmp_path),"Check",2))
    await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not (tmp_path/"integration").exists()
    assert all(json.loads(line)["kind"] != "conclusion" for file in (tmp_path/"bridge").glob("*.jsonl") for line in file.read_text().splitlines())


@pytest.mark.asyncio
async def test_council_slots_release_after_cancellation(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    entered = 0
    async def blocked(*args):
        nonlocal entered
        entered += 1
        if entered == 2: started.set()
        await release.wait()
        return {"state":"HOLD"}
    monkeypatch.setattr(integration, "conclude", blocked)
    app = app_for(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        one = asyncio.create_task(client.post("/api/integration/conclude", json={"message":"one"}))
        two = asyncio.create_task(client.post("/api/integration/conclude", json={"message":"two"}))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert (await client.post("/api/integration/conclude",json={"message":"three"})).status_code == 429
            one.cancel()
            with pytest.raises(asyncio.CancelledError): await one
            assert len(app.state.councils) == 1
        finally:
            release.set()
            await asyncio.gather(one, two, return_exceptions=True)
        assert not app.state.councils
