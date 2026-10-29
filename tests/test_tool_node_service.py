"""Node service tool safety, per-agent confinement and lifecycle edge tests (no Docker required)."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import tools_introspect as ti
from avrone.tools import Tool, ToolRegistry


def run(coro):
    return asyncio.run(coro)


class FakeRunner:
    kind = "docker"

    def __init__(self, workspace, responses=None):
        self.workspace = Path(workspace)
        self.responses = dict(responses or {})
        self.calls = []

    async def run(self, cmd, timeout=None, cwd=None):
        self.calls.append((cmd, timeout, Path(cwd) if cwd is not None else None))
        value = self.responses.get(cmd)
        if callable(value):
            value = value(cmd, timeout, cwd)
        if value is None:
            return SimpleNamespace(exit_code=127, stdout="", stderr="not configured", timed_out=False)
        return value


def make_ctx(tmp_path, monkeypatch, runner_kind="docker", responses=None, agent_id="alpha"):
    workspace = tmp_path / "workspace"
    home = workspace / "agents" / "alpha"
    home.mkdir(parents=True)
    runner = FakeRunner(workspace, responses)
    runner.kind = runner_kind
    registry = ToolRegistry()

    async def fake_fs_write(ctx, args):
        assert args["path"] == "node-server.js"
        target = Path(ctx.agent_dir) / args["path"]
        if target.is_symlink():
            return "ERROR: symlink refused"
        target.write_text(args["content"], encoding="utf-8")
        return f"wrote node-server.js ({len(args['content'])} bytes)"

    registry.register(Tool("fs_write", "fake quota-enforced file writer", '{"path": str, "content": str}', fake_fs_write,
                           source="builtin", schema={"required": {"path": "str", "content": "str"}}))
    ti.register_introspect_tools(registry)
    ctx = SimpleNamespace(
        core=SimpleNamespace(
            runner=runner,
            settings=SimpleNamespace(workspace=workspace, data_dir=tmp_path / "data",
                                     provider="ollama", llm_mode="mock"),
            tools=registry,
        ),
        run_id="node-service-test",
        name="Builder-1",
        depth=0,
        agent_id=agent_id,
        agent_dir=home,
    )
    return SimpleNamespace(ctx=ctx, runner=runner, registry=registry, workspace=workspace, home=home)


def call(env, **args):
    return run(env.registry.call(env.ctx, "node_service", args))


def as_json(output):
    return json.loads(output)


def test_node_service_template_is_loopback_only_and_bounded():
    source = ti.NODE_SERVER_SOURCE
    assert len(source.encode("utf-8")) <= ti.NODE_SERVICE_MAX_SOURCE_BYTES
    assert 'const HOST = "127.0.0.1";' in source
    assert "server.listen(PORT, HOST)" in source
    assert 'req.url === "/health"' in source
    assert 'req.url === "/__stop"' in source
    assert "0.0.0.0" not in source and "--privileged" not in source
    assert 'service: "avrone-agent-node"' in source
    assert "stopping: true" in source


def test_scaffold_uses_fs_write_and_is_idempotent(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch)
    first = as_json(call(env, action="scaffold", format="json"))
    target = env.home / "node-server.js"
    assert first["ok"] and first["status"] == "scaffolded"
    assert target.read_text(encoding="utf-8") == ti.NODE_SERVER_SOURCE
    assert env.runner.calls == []

    second = as_json(call(env, action="scaffold", format="json"))
    assert second["ok"] and second["status"] == "scaffolded"
    assert "already matches" in second["message"]
    assert len(env.runner.calls) == 0


def test_scaffold_never_overwrites_custom_file(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch)
    target = env.home / "node-server.js"
    target.write_text("custom project; do not replace", encoding="utf-8")
    result = as_json(call(env, action="scaffold", format="json"))
    assert not result["ok"] and result["status"] == "refused"
    assert "different content" in result["message"]
    assert target.read_text(encoding="utf-8") == "custom project; do not replace"


def test_scaffold_refuses_symlink_target_without_following_it(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch)
    elsewhere = tmp_path / "outside.js"
    elsewhere.write_text("preserve", encoding="utf-8")
    target = env.home / "node-server.js"
    os.symlink(elsewhere, target)
    result = as_json(call(env, action="scaffold", format="json"))
    assert not result["ok"] and "symlink" in result["message"]
    assert elsewhere.read_text(encoding="utf-8") == "preserve"
    assert env.runner.calls == []


def test_node_service_refuses_foreign_agent_home(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch, agent_id="another-agent")
    result = call(env, action="scaffold")
    assert result.startswith("ERROR:")
    assert "own confined agent home" in result
    assert not (env.home / "node-server.js").exists()


def test_start_requires_persistent_docker_without_running_commands(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch, runner_kind="local")
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    result = as_json(call(env, action="start", format="json"))
    assert not result["ok"] and result["status"] == "refused"
    assert "persistent" in result["message"]
    assert env.runner.calls == []
    assert not (env.home / "node-server.js").exists()


def test_start_requires_operator_enabled_persistent_mode(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch, runner_kind="docker")
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "off")
    result = as_json(call(env, action="start", format="json"))
    assert not result["ok"]
    assert env.runner.calls == []


def test_start_fails_closed_when_node_is_unavailable(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch, responses={
        "node --version": SimpleNamespace(exit_code=127, stdout="", stderr="private path", timed_out=False)
    })
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    result = as_json(call(env, action="start", format="json"))
    assert not result["ok"] and result["status"] == "node_unavailable"
    assert "private path" not in json.dumps(result)
    assert [c[0] for c in env.runner.calls] == ["node --version"]
    assert not (env.home / "node-server.js").exists()


def test_start_scaffolds_then_runs_only_fixed_probe_command(tmp_path, monkeypatch):
    responses = {
        "node --version": SimpleNamespace(exit_code=0, stdout="v22.11.0\n", stderr="", timed_out=False),
        ti._NODE_START_COMMAND: SimpleNamespace(exit_code=0, stdout="started", stderr="", timed_out=False),
    }
    env = make_ctx(tmp_path, monkeypatch, responses=responses)
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")

    result = as_json(call(env, action="start", format="json"))
    assert result["ok"] and result["status"] == "started"
    assert result["url"] == "http://127.0.0.1:8765"
    assert "no host port is published" in result["scope"]
    assert (env.home / "node-server.js").read_text(encoding="utf-8") == ti.NODE_SERVER_SOURCE
    assert [c[0] for c in env.runner.calls] == ["node --version", ti._NODE_START_COMMAND]
    assert all(timeout == 5 and cwd == env.home for _, timeout, cwd in env.runner.calls)


@pytest.mark.parametrize("action,command,status", [
    ("status", ti._NODE_HEALTH_PROBE, "running"),
    ("stop", ti._NODE_STOP_PROBE, "stopped"),
])
def test_status_and_stop_use_fixed_loopback_probes(tmp_path, monkeypatch, action, command, status):
    env = make_ctx(tmp_path, monkeypatch, responses={
        command: SimpleNamespace(exit_code=0, stdout='{"ok":true,"service":"avrone-agent-node"}',
                                 stderr="", timed_out=False)
    })
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    result = as_json(call(env, action=action, format="json"))
    assert result["ok"] and result["status"] == status
    assert [c[0] for c in env.runner.calls] == [command]
    assert all(timeout == 5 and cwd == env.home for _, timeout, cwd in env.runner.calls)


def test_status_reports_no_server_without_claiming_it_is_running(tmp_path, monkeypatch):
    env = make_ctx(tmp_path, monkeypatch, responses={
        ti._NODE_HEALTH_PROBE: SimpleNamespace(exit_code=2, stdout="", stderr="", timed_out=False)
    })
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    result = as_json(call(env, action="status", format="json"))
    assert result["ok"] and result["status"] == "not_running"
    assert result["url"] is None


@pytest.mark.parametrize("args", [
    {"action": "execute", "format": "json"},
    {"action": "status", "format": "xml"},
])
def test_node_service_rejects_unknown_action_and_format(tmp_path, monkeypatch, args):
    env = make_ctx(tmp_path, monkeypatch)
    result = call(env, **args)
    assert result.startswith("ERROR:")
    assert env.runner.calls == []
