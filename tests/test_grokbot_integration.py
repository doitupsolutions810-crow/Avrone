import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from avrone import grokbot, integration
from avrone.codex import Codex


def response(answer="Independent result", correlation_id="council-test", **changes):
    data = {"ok": True, "answer": answer, "receipt": {"run_id": "research-test", "agent": "grok-researcher",
            "correlation_id": correlation_id, "answer_sha256": hashlib.sha256(answer.encode()).hexdigest()}}
    data.update(changes)
    return data


@pytest.fixture
def peer(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000")
    monkeypatch.setenv("GROK_BRIDGE_TOKEN", "private-bridge-token")
    return monkeypatch


@pytest.mark.asyncio
async def test_fixed_research_endpoint_auth_and_correlated_receipt(peer):
    def handler(req):
        assert str(req.url) == "http://grok-bot:3000/api/bridge/research"
        assert req.headers["authorization"] == "Bearer private-bridge-token"
        assert json.loads(req.content) == {"message": "Check", "correlation_id": "council-test"}
        return httpx.Response(200, json=response())
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    result = await grokbot.ask("Check", correlation_id="council-test")
    assert result["ok"] and result["receipt"]["run_id"] == "research-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [response(correlation_id="stale"), response(receipt={}),
                                      response(answer="Changed", receipt=response()["receipt"]),
                                      response(ok=False), {"ok": True}, []])
async def test_rejects_missing_stale_or_tampered_evidence(peer, body):
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    assert (await grokbot.ask("Check", correlation_id="council-test"))["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///tmp", "http://user:secret@grok:3000", "http://grok/admin", "http://grok?secret=x", "http://[bad", "http://grok:bad"])
async def test_rejects_non_origin_targets(peer, url):
    peer.setenv("AVRONE_GROKBOT_URL", url)
    result = await grokbot.ask("Check")
    assert not result["ok"] and "secret" not in result["error"]


@pytest.mark.asyncio
async def test_redirect_status_and_transport_details_are_not_leaked(peer):
    for code in [302, 401, 503]:
        peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _, c=code: httpx.Response(c, text="private-upstream-body")))
        result = await grokbot.ask("Check")
        assert not result["ok"] and str(code) in result["error"]
        assert "private-upstream-body" not in result["error"]
    def fail(_):
        raise httpx.ConnectError("private-host:secret-token")
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(fail))
    assert (await grokbot.ask("Check"))["error"] == "ConnectError"


@pytest.mark.asyncio
async def test_streamed_body_bound_and_prompt_validation(peer):
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (grokbot.MAX_BYTES + 1)
    peer.setattr(grokbot, "HTTP_TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(200, stream=Large())))
    assert "64 KiB" in (await grokbot.ask("Check"))["error"]
    for prompt in [None, {}, "", " " * 10, "x" * 8001]:
        assert not (await grokbot.ask(prompt))["ok"]


class Core:
    def __init__(self, tmp_path, mode="live", complete=True):
        self.settings = SimpleNamespace(data_dir=tmp_path, llm_mode=mode)
        self.codex = Codex(tmp_path)
        self.complete = complete

    async def squad(self, message, n, run_id):
        return {"run_id": run_id, "answer": "AVRONE report" if self.complete else "Model backend error: unavailable",
                "agents": [{"role": "Worker", "result": "Measured report"}] if self.complete else []}

    async def _chat(self, *args, **kwargs):
        return "Lead compared both independent reports."


@pytest.mark.asyncio
async def test_council_receipt_persists_and_binds_both_reports(tmp_path, monkeypatch):
    async def ask(_, correlation_id):
        return response(correlation_id=correlation_id)
    monkeypatch.setattr(grokbot, "ask", ask)
    result = await integration.conclude(Core(tmp_path), "Check", 2)
    assert result["state"] == "COMPLETE"
    stored = json.loads((tmp_path / "integration" / (result["run_id"] + ".json")).read_text())
    digest = stored.pop("receipt_sha256")
    assert digest == hashlib.sha256(json.dumps(stored, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    from avrone.bridge import load_posts
    posts = load_posts(tmp_path, result["run_id"])
    conclusion = next(p for p in posts if p["kind"] == "conclusion")
    assert len(conclusion["refs"]) == 2


@pytest.mark.asyncio
async def test_missing_peer_or_failed_squad_stays_hold(tmp_path, monkeypatch):
    async def unavailable(*a, **kw):
        return {"ok": False, "error": "unavailable"}
    monkeypatch.setattr(grokbot, "ask", unavailable)
    result = await integration.conclude(Core(tmp_path), "Check", 2)
    assert result["state"] == "HOLD"
    assert (await integration.conclude(Core(tmp_path, complete=False), "Check", 2))["state"] == "HOLD"


@pytest.mark.asyncio
async def test_mock_cannot_produce_live_receipt(tmp_path):
    with pytest.raises(ValueError, match="live inference"):
        await integration.conclude(Core(tmp_path, mode="mock"), "Check", 2)
    assert not (tmp_path / "integration").exists()


def test_failed_worker_is_not_promoted_even_if_lead_returns_an_answer():
    assert not integration._complete({"answer": "Summary", "agents": [{"result": "[Worker-1 failed: backend unavailable]"}]})
    assert not integration._complete({"answer": "Summary", "agents": [{"result": "[Worker-1 ran out of turns (8 steps) without a final answer]"}]})


def test_routes_keep_existing_access_guard(tmp_path):
    from fastapi import HTTPException
    app = FastAPI()
    def require_local(request, token):
        if token != "allowed":
            raise HTTPException(403, "denied")
    integration.install_routes(app, lambda: Core(tmp_path, mode="mock"), require_local, SimpleNamespace())
    with TestClient(app) as client:
        assert client.get("/api/integration").status_code == 403
        assert client.post("/api/integration/conclude", json={"message": "hello"}).status_code == 403
        assert client.post("/api/integration/conclude", json={"message": "hello"}, headers={"X-Admin-Token": "allowed"}).status_code == 409
