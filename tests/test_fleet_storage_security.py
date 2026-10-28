from unittest.mock import MagicMock

import pytest

from avrone.fleet import FleetManager


def _core():
    core = MagicMock()
    core.active = {}
    core.tools.names.return_value = []
    core.runner.kind = "local"
    core.codex.emit = MagicMock()
    return core


def test_fleet_registry_refuses_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "dispatch-root")
    target = tmp_path / "outside.json"
    target.write_text('{"pwned":true}', encoding="utf-8")
    (tmp_path / "fleet.json").symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        FleetManager(_core(), tmp_path)
    assert target.read_text(encoding="utf-8") == '{"pwned":true}'


def test_fleet_registry_atomic_save_does_not_leave_temp_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "dispatch-root")
    fm = FleetManager(_core(), tmp_path)
    result = fm.register({
        "node_id": "edge-1",
        "url": "https://edge.example:8710",
        "capabilities": {"runner": "docker"},
        "children": [],
        "load": 0,
        "presence": {"agents": []},
    })
    assert result["ok"] is True
    assert (tmp_path / "fleet.json").exists()
    assert not list(tmp_path.glob(".fleet-*.tmp"))


def test_fleet_registration_caps_persisted_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "dispatch-root")
    fm = FleetManager(_core(), tmp_path)
    with pytest.raises(ValueError, match="presence data must not exceed"):
        fm.register({
            "node_id": "edge-1",
            "url": "https://edge.example:8710",
            "presence": {"snapshot": "x" * 17_000},
        })


def test_fleet_registry_skips_future_timestamp_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "dispatch-root")
    (tmp_path / "fleet.json").write_text(
        '{"node_id":"dispatch-root","nodes":{"edge-1":{"node_id":"edge-1",'
        '"url":"https://edge.example:8710","last_seen":99999999999,"load":0}}}',
        encoding="utf-8",
    )
    fm = FleetManager(_core(), tmp_path)
    assert "edge-1" not in fm.nodes


def test_fleet_registry_skips_malformed_peer_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_NODE_ID", "dispatch-root")
    (tmp_path / "fleet.json").write_text(
        '{"node_id":"dispatch-root","nodes":{'
        '"bad-load":{"url":"https://bad.example","last_seen":100,"load":"high"},'
        '"bad-children":{"url":"https://bad2.example","last_seen":100,"load":0,"children":"edge"},'
        '"good":{"url":"https://good.example","last_seen":100,"load":1,"children":["edge-1"]}}}',
        encoding="utf-8",
    )
    fm = FleetManager(_core(), tmp_path)
    assert set(fm.nodes) == {"good"}
    assert fm.nodes["good"]["children"] == ["edge-1"]
