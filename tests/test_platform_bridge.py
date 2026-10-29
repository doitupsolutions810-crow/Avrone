from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from avrone.plugins import load_plugins
from avrone.tools import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


def _load_bridge():
    registry = ToolRegistry()
    reports = load_plugins(registry, None, directory=ROOT / "plugins", include_entry_points=False)
    report = next(r for r in reports if r["name"] == "platform_bridge")
    assert report["error"] is None
    module = importlib.import_module("avrone_plugin_platform_bridge")
    return registry, module


def _call(registry, name, args=None):
    return asyncio.run(registry.get(name).fn(None, args or {}))


def test_bridge_registers_inventory_read_and_gated_write(monkeypatch):
    registry, _module = _load_bridge()
    assert {"platform_bridge_inventory", "platform_bridge_read", "platform_bridge_submit_job"} <= set(registry.names())
    monkeypatch.delenv("AVRONE_PLATFORM_TARGETS_JSON", raising=False)
    monkeypatch.delenv("AVRONE_PLATFORM_WRITE_ENABLED", raising=False)
    inventory = json.loads(_call(registry, "platform_bridge_inventory"))
    assert inventory["ok"] is True
    assert inventory["targets"]["grokbot"]["configured"] is False
    assert inventory["write_enabled"] is False


def test_read_rejects_unknown_target_and_resource_without_network(monkeypatch):
    registry, module = _load_bridge()
    async def forbidden(*args, **kwargs):
        raise AssertionError("request must not be made")
    monkeypatch.setattr(module, "_request", forbidden)
    unknown_target = _call(registry, "platform_bridge_read", {"target": "https://example.com", "resource": "status"})
    unknown_resource = _call(registry, "platform_bridge_read", {"target": "grokbot", "resource": "shell"})
    assert unknown_target.startswith("ERROR:")
    assert unknown_resource.startswith("ERROR:")


def test_read_uses_fixed_grokbot_resource(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({"grokbot": "http://grok-bot:3000"}))
    calls = []
    async def fake_request(target, method, path, body=None):
        calls.append((target, method, path, body))
        return json.dumps({"ok": True, "fixture": True})
    monkeypatch.setattr(module, "_request", fake_request)
    result = _call(registry, "platform_bridge_read", {"target": "grokbot", "resource": "status"})
    assert json.loads(result)["fixture"] is True
    assert calls == [("grokbot", "GET", "/api/status", None)]


def test_job_submission_is_disabled_by_default(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({"control704": "http://control704:9100"}))
    monkeypatch.delenv("AVRONE_PLATFORM_WRITE_ENABLED", raising=False)
    async def forbidden(*args, **kwargs):
        raise AssertionError("write request must not be made")
    monkeypatch.setattr(module, "_request", forbidden)
    result = _call(registry, "platform_bridge_submit_job", {"goal": "review the system"})
    assert result.startswith("ERROR:")
    assert "disabled" in result


def test_job_submission_only_targets_control704_jobs(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({"control704": "http://control704:9100"}))
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    calls = []
    async def fake_request(target, method, path, body=None):
        calls.append((target, method, path, body))
        return json.dumps({"ok": True, "job_id": "fixture-job"})
    monkeypatch.setattr(module, "_request", fake_request)
    result = _call(registry, "platform_bridge_submit_job", {
        "goal": "Analyze integration gaps", "priority": 5, "tenant_id": "tenant-demo"
    })
    assert json.loads(result)["job_id"] == "fixture-job"
    assert calls == [("control704", "POST", "/jobs", {
        "goal": "Analyze integration gaps",
        "priority": 5,
        "source": "avrone-platform-bridge",
        "tenant_id": "tenant-demo",
    })]


def test_job_submission_validates_input_before_network(monkeypatch):
    registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({"control704": "http://control704:9100"}))
    monkeypatch.setenv("AVRONE_PLATFORM_WRITE_ENABLED", "true")
    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid input must not reach network")
    monkeypatch.setattr(module, "_request", forbidden)
    bad_priority = _call(registry, "platform_bridge_submit_job", {"goal": "x", "priority": True})
    empty_goal = _call(registry, "platform_bridge_submit_job", {"goal": "  "})
    too_long = _call(registry, "platform_bridge_submit_job", {"goal": "x" * 4001})
    assert bad_priority.startswith("ERROR:")
    assert empty_goal.startswith("ERROR:")
    assert too_long.startswith("ERROR:")


def test_targets_reject_non_http_and_userinfo(monkeypatch):
    _registry, module = _load_bridge()
    monkeypatch.setenv("AVRONE_PLATFORM_TARGETS_JSON", json.dumps({
        "grokbot": "ftp://grok-bot:3000",
        "control704": "http://user:pass@control704:9100",
        "great_scale": "http://great-scale:3000",
    }))
    assert module._targets() == {"great_scale": "http://great-scale:3000"}
