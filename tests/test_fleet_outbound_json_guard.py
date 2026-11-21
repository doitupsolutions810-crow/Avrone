"""Outbound fleet signing must reject invalid local payloads without transport."""
from __future__ import annotations

import json
import math
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, sign


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "strict-transport-token")
    monkeypatch.setenv("AVRONE_NODE_ID", "source")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    core = MagicMock()
    core.active = {}
    core.tools.names.return_value = []
    core.runner.kind = "local"
    return FleetManager(core, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    ["not-a-dict"],
    {"bad": math.nan},
    {"bad": math.inf},
    {"bad": -math.inf},
    {"bad": "\ud800"},
    {"bad": 10 ** 5000},
], ids=["array", "nan", "inf", "minus-inf", "lone-surrogate", "huge-integer"])
async def test_rejects_invalid_outbound_body_before_network(node, monkeypatch, bad):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="peer request must"):
        await node._post("http://child.test", "/api/fleet/run", bad)
    assert not calls


@pytest.mark.asyncio
async def test_signed_non_ascii_outbound_json_round_trip(node, monkeypatch):
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    record = {"prompt": "worker \U0001f9ed \u20ac \u00e9"}
    assert await node._post("http://child.test", "/api/fleet/run", record) == {"ok": True}
    req = sent[0]
    assert json.loads(req.content.decode("utf-8")) == record
    assert req.content.decode("utf-8") == json.dumps(record, ensure_ascii=False, allow_nan=False)
    signature = sign(node.token, "POST", "/api/fleet/run", req.content,
                     req.headers["x-avrone-ts"], req.headers["x-avrone-nonce"])
    assert signature["x-avrone-sig"] == req.headers["x-avrone-sig"]
