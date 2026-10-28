import asyncio
import signal

import pytest

from avrone.config import Settings
from avrone.sovereign import CommandResult, DockerRunner, LocalRunner


@pytest.mark.asyncio
async def test_local_runner_timeout_kills_process_group(monkeypatch, tmp_path):
    seen = {}

    class FakeProcess:
        pid = 54321
        returncode = -9

        async def communicate(self):
            raise asyncio.TimeoutError()

        async def wait(self):
            seen["waited"] = True

        def kill(self):
            seen["process_kill"] = True

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("avrone.sovereign.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("avrone.sovereign.os.killpg", lambda pid, sig: seen.update(killpg=(pid, sig)))
    runner = LocalRunner(Settings(runner="local", workspace=tmp_path))
    result = await runner._exec(["bash", "-lc", "sleep 30"], "sleep 30", 1)
    assert result.timed_out is True
    assert seen["killpg"] == (54321, signal.SIGKILL)
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["waited"] is True


@pytest.mark.asyncio
async def test_docker_runner_places_timeout_inside_container(monkeypatch, tmp_path):
    runner = DockerRunner(Settings(runner="docker", workspace=tmp_path))
    runner.container = "test-container"
    seen = {}

    async def fake_exec(argv, cmd, timeout):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return CommandResult(cmd, 0, "ok", "")

    monkeypatch.setattr(runner, "_exec", fake_exec)
    result = await runner.run("sleep 30", timeout=7)
    assert result.exit_code == 0
    assert seen["argv"][:6] == [
        "docker", "exec", "test-container", "timeout", "-s", "KILL"
    ]
    assert seen["argv"][6] == "7s"
    assert seen["timeout"] == 12
