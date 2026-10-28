import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from avrone.fleet import FleetManager, sign, WINDOW_S, ID_RE


@pytest.fixture
def mock_core():
    core = MagicMock()
    core.active = {}
    core.tools.names.return_value = ["test_tool"]
    core.runner.kind = "local"
    core.codex.emit = MagicMock()
    return core


def test_sign_and_verify_good(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    body = b'{"prompt":"hi"}'
    headers = sign(fm.token, "POST", "/api/fleet/run", body)
    fm.verify("POST", "/api/fleet/run", body, headers)
    assert len(fm._nonces) == 1


def test_verify_tampered_signature(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    body = b'{"prompt":"hi"}'
    headers = sign(fm.token, "POST", "/api/fleet/run", body)
    headers["x-avrone-sig"] = "0000000000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(PermissionError, match="bad signature"):
        fm.verify("POST", "/api/fleet/run", body, headers)


def test_verify_stale_timestamp(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    body = b'{"prompt":"hi"}'
    ts = str(int(time.time()) - WINDOW_S - 10)
    headers = sign(fm.token, "POST", "/api/fleet/run", body, ts=ts)
    with pytest.raises(PermissionError, match="stale request"):
        fm.verify("POST", "/api/fleet/run", body, headers)


def test_verify_replay_nonce(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    body = b'{"prompt":"hi"}'
    headers = sign(fm.token, "POST", "/api/fleet/run", body)
    fm.verify("POST", "/api/fleet/run", body, headers)
    with pytest.raises(PermissionError, match="replayed request"):
        fm.verify("POST", "/api/fleet/run", body, headers)


def test_node_id_validation(mock_core, tmp_path):
    os.environ["AVRONE_NODE_ID"] = "bad@id"
    with pytest.raises(ValueError):
        FleetManager(mock_core, tmp_path)
    del os.environ["AVRONE_NODE_ID"]


def test_registry_add_list(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    fm.allow = set()
    payload = {
        "node_id": "child1",
        "url": "http://127.0.0.1:8001",
        "capabilities": {"tools": ["foo"]},
        "children": [],
        "load": 0,
        "presence": {}
    }
    result = fm.register(payload)
    assert result["ok"] is True
    assert result["parent"] == fm.node_id
    nodes = fm.list_nodes()
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "child1"
    saved = json.loads(fm.path.read_text())
    assert "child1" in saved["nodes"]
    fm.register(payload)
    assert len(fm.nodes) == 1


def test_registry_rejects_invalid(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    fm.allow = {"allowed1"}
    with pytest.raises(ValueError, match="invalid node_id"):
        fm.register({"node_id": "", "url": "http://ex.com"})
    with pytest.raises(ValueError, match="node_id collides"):
        fm.register({"node_id": fm.node_id, "url": "http://ex.com"})
    with pytest.raises(PermissionError, match="not in AVRONE_FLEET_ALLOW"):
        fm.register({"node_id": "badguy", "url": "http://ex.com"})
    with pytest.raises(ValueError, match="url must be http"):
        fm.register({"node_id": "allowed1", "url": "ftp://bad"})

@pytest.mark.asyncio
async def test_pick_and_dispatch_route_hop_limit(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    fm.max_hops = 3
    fm.nodes = {
        "router1": {"node_id": "router1", "url": "http://r1", "last_seen": time.time(), "load": 0},
        "edge7": {"node_id": "edge7", "url": "http://e7", "last_seen": time.time() - 1000, "load": 0},
    }
    n = fm.pick("router1")
    assert n["node_id"] == "router1"
    n = fm.pick("auto")
    assert n["node_id"] == "router1"
    with patch.object(fm, "_post", new_callable=AsyncMock, return_value={"ok": True}):
        with pytest.raises(PermissionError, match="max fleet hops"):
            await fm.dispatch(["router1", "edge7"], "chat", "hi", hops=3)
    with pytest.raises(RuntimeError, match="unknown node"):
        fm.pick("missing")


def test_info_status_allowed(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    fm.allow = {"a1", "a2"}
    assert fm.allowed("a1") is True
    assert fm.allowed("outsider") is False
    fm.nodes = {"n1": {"last_seen": time.time(), "load": 5}}
    info = fm.info()
    assert info["node_id"] == fm.node_id
    assert info["children"] == 1
    assert info["active_agents"] == 0
    statuses = [n["status"] for n in fm.list_nodes()]
    assert "online" in statuses
    assert fm.status_of(fm.nodes["n1"]) == "online"

def test_id_re_pattern():
    assert ID_RE.match("valid-node-42")
    assert not ID_RE.match("Invalid")
    assert not ID_RE.match("-start")
    assert not ID_RE.match("too-long-" + "x"*60)

@pytest.mark.asyncio
async def test_start_stop_heartbeat(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = "secret123"
    fm.parent = "http://parent.example.com"
    fm.public_url = "http://self.example.com"
    fm.start()
    assert fm._task is not None
    await fm.stop()
    assert fm._task is None


def test_status_of_all_freshness_bands(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.heartbeat_s = 10
    now = time.time()
    assert fm.status_of({"last_seen": now - 2}) == "online"
    assert fm.status_of({"last_seen": now - 40}) == "degraded"
    assert fm.status_of({"last_seen": now - 120}) == "offline"


def test_verify_disabled_and_bad_timestamp(mock_core, tmp_path):
    fm = FleetManager(mock_core, tmp_path)
    fm.token = ""
    with pytest.raises(PermissionError, match="fleet disabled"):
        fm.verify("POST", "/api/fleet/run", b"{}", {})
    fm.token = "secret123"
    with pytest.raises(PermissionError, match="bad timestamp"):
        fm.verify("POST", "/api/fleet/run", b"{}", {"x-avrone-ts": "not-a-time"})
