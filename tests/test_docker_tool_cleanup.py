from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.sovereign import CommandResult


@pytest.mark.asyncio
async def test_docker_tool_kills_named_container_after_host_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(tx.shutil, "which", lambda _name: "/usr/bin/docker")
    settings = Settings(workspace=tmp_path, allow_network=False, agents_can_shell=True, command_timeout=5)
    seen = []

    async def fake_exec(argv, cmd, timeout):
        seen.append((argv, cmd, timeout))
        if argv[1:2] == ["run"]:
            return CommandResult(cmd, -1, "", "command timed out; process group terminated", timed_out=True)
        return CommandResult(cmd, 0, "", "")

    runner = SimpleNamespace(_exec=fake_exec)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=settings, runner=runner))

    result = await tx.t_docker_run(ctx, {"image": "python:3.12-slim", "cmd": "sleep 90"})
    assert "TIMEOUT" in result
    assert len(seen) == 2
    run_argv, _, run_timeout = seen[0]
    cleanup_argv, _, cleanup_timeout = seen[1]
    container_name = run_argv[run_argv.index("--name") + 1]
    assert cleanup_argv == ["docker", "rm", "-f", container_name]
    assert "--network" in run_argv and "none" in run_argv
    assert run_timeout == 185
    assert cleanup_timeout == 15
    assert "[command omitted]" in seen[0][1]
    assert "sleep 90" not in seen[0][1]


@pytest.mark.asyncio
async def test_docker_tool_reports_cleanup_failure_after_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(tx.shutil, "which", lambda _name: "/usr/bin/docker")
    settings = Settings(workspace=tmp_path, allow_network=True, agents_can_shell=True, command_timeout=3)
    calls = 0

    async def fake_exec(argv, cmd, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return CommandResult(cmd, -1, "", "timed out", timed_out=True)
        return CommandResult(cmd, 1, "", "daemon unreachable")

    ctx = SimpleNamespace(core=SimpleNamespace(
        settings=settings, runner=SimpleNamespace(_exec=fake_exec)
    ))
    result = await tx.t_docker_run(ctx, {"image": "alpine:latest", "cmd": "sleep 90"})
    assert "cleanup warning" in result
