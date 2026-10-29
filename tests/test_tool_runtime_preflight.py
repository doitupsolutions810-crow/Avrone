"""Read-only runtime_preflight tool contract and edge-case tests."""
import asyncio
import json
from types import SimpleNamespace

from avrone import tools_extra as tx
from avrone import tools_introspect as ti
from avrone.tools import builtin_registry


def make_ctx(workspace, runner_kind="local"):
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    settings = SimpleNamespace(
        workspace=workspace,
        provider="ollama",
        llm_mode="mock",
        llm_base_url="http://user:password@example.invalid/v1",
        llm_api_key="api-secret-must-not-appear",
        admin_token="admin-secret-must-not-appear",
    )
    runner = SimpleNamespace(kind=runner_kind, workspace=workspace)
    core = SimpleNamespace(settings=settings, runner=runner, tools=reg)
    ctx = SimpleNamespace(core=core, run_id="r-preflight", name="Worker", depth=0)
    return reg, ctx


def call(reg, ctx, **args):
    return asyncio.run(reg.call(ctx, "runtime_preflight", args))


def test_runtime_preflight_is_read_only_and_does_not_expose_credentials(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(ti.shutil, "which", lambda name: None)
    before = set(tmp_path.iterdir())
    reg, ctx = make_ctx(workspace)
    result = json.loads(call(reg, ctx, format="json"))

    assert result["ready"] is True
    assert result["runner"] == "local"
    assert result["workspace"] == {"configured": True, "exists": True, "writable": True}
    assert result["model"] == {
        "provider": "ollama", "mode": "mock", "base_url_set": True, "api_key_set": True
    }
    assert result["admin"]["enabled"] is True
    assert result["agent_container"]["mode"] in {"off", "persistent"}
    assert result["agent_container"]["persistent_configured"] is False
    output = json.dumps(result)
    assert str(workspace) not in output
    assert "password" not in output and "api-secret-must-not-appear" not in output
    assert "admin-secret-must-not-appear" not in output
    assert set(tmp_path.iterdir()) == before
    assert "runtime_preflight (read-only; ready=yes)" in call(reg, ctx)
    assert call(reg, ctx, format="yaml").startswith("ERROR: format must be")


def test_runtime_preflight_missing_workspace_and_docker_is_not_ready(tmp_path, monkeypatch):
    workspace = tmp_path / "does-not-exist"
    monkeypatch.setattr(ti.shutil, "which", lambda name: None)
    reg, ctx = make_ctx(workspace, runner_kind="docker")
    result = json.loads(call(reg, ctx, format="json"))
    assert not result["ready"]
    assert not result["workspace"]["exists"] and not result["workspace"]["writable"]
    assert result["checks"]["docker_available"] is False
    assert not workspace.exists()  # the preflight never creates the workspace



def test_runtime_preflight_reports_persistent_agent_container_without_exposing_identity(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    monkeypatch.setattr(ti.shutil, "which", lambda name: None)
    reg, ctx = make_ctx(workspace, runner_kind="docker")
    ctx.agent_id = "researcher"
    ctx.core.runner.agent_pool = SimpleNamespace(node="node-a", homes={})

    result = json.loads(call(reg, ctx, format="json"))
    assert result["agent_container"] == {
        "mode": "persistent",
        "persistent_configured": True,
        "agent_id_present": True,
        "live_container_for_agent": False,
    }
    assert "researcher" not in json.dumps(result)


def test_node_runtime_probes_fixed_commands_inside_agent_home(tmp_path):
    class Runner:
        kind = "docker"
        workspace = tmp_path

        def __init__(self):
            self.calls = []

        async def run(self, cmd, timeout=None, cwd=None):
            self.calls.append((cmd, timeout, cwd))
            if cmd == "node --version":
                return SimpleNamespace(exit_code=0, stdout="unexpected banner with credential text\nv22.11.0\n", stderr="")
            if cmd == "npm --version":
                return SimpleNamespace(exit_code=127, stdout="", stderr="secret path should not leak")
            raise AssertionError(f"unexpected command: {cmd}")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reg, ctx = make_ctx(workspace, runner_kind="docker")
    ctx.core.runner = Runner()
    ctx.agent_dir = workspace / "agents" / "worker"
    ctx.agent_dir.mkdir(parents=True)

    async def run_probe():
        return await reg.call(ctx, "node_runtime", {"format": "json"})

    report = json.loads(asyncio.run(run_probe()))
    assert report["node"] == {"available": True, "version": "v22.11.0", "status": "available"}
    assert report["npm"]["status"] == "missing_or_unavailable" and not report["npm"]["available"]
    assert all(cwd == ctx.agent_dir and timeout == 5 for _, timeout, cwd in ctx.core.runner.calls)
    assert [cmd for cmd, _, _ in ctx.core.runner.calls] == ["node --version", "npm --version"]
    assert "secret path should not leak" not in json.dumps(report)
    assert "unexpected banner with credential text" not in json.dumps(report)


def test_node_runtime_reports_timeout_and_invalid_format(tmp_path):
    class Runner:
        kind = "local"
        workspace = tmp_path

        async def run(self, cmd, timeout=None, cwd=None):
            return SimpleNamespace(exit_code=-1, stdout="", stderr="", timed_out=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reg, ctx = make_ctx(workspace)
    ctx.core.runner = Runner()

    async def run_probe():
        a = await reg.call(ctx, "node_runtime", {"format": "json"})
        b = await reg.call(ctx, "node_runtime", {"format": "xml"})
        return a, b

    text, bad = asyncio.run(run_probe())
    assert json.loads(text)["node"]["status"] == "timeout"
    assert json.loads(text)["npm"]["status"] == "timeout"
    assert bad.startswith("ERROR: format must be")
