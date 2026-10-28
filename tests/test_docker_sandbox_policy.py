import pytest

from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.sovereign import CommandResult, DockerRunner, _docker_sandbox_args


def test_docker_sandbox_applies_least_privilege_and_resource_caps(monkeypatch):
    monkeypatch.setenv("AVRONE_DOCKER_MEMORY_LIMIT", "4g")
    monkeypatch.setenv("AVRONE_DOCKER_CPU_LIMIT", "1.5")
    monkeypatch.setenv("AVRONE_DOCKER_PIDS_LIMIT", "256")
    args = _docker_sandbox_args(allow_network=False)
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges:true" in args
    assert args[args.index("--memory") + 1] == "4g"
    assert args[args.index("--cpus") + 1] == "1.5"
    assert args[args.index("--pids-limit") + 1] == "256"
    assert args[args.index("--network") + 1] == "none"


def test_docker_sandbox_invalid_or_out_of_range_limits_fail_back_to_safe_defaults(monkeypatch):
    monkeypatch.setenv("AVRONE_DOCKER_MEMORY_LIMIT", "999g")
    monkeypatch.setenv("AVRONE_DOCKER_CPU_LIMIT", "999")
    monkeypatch.setenv("AVRONE_DOCKER_PIDS_LIMIT", "999999")
    args = _docker_sandbox_args(allow_network=True)
    assert args[args.index("--memory") + 1] == "2g"
    assert args[args.index("--cpus") + 1] == "2.0"
    assert args[args.index("--pids-limit") + 1] == "512"
    assert "--network" not in args


@pytest.mark.asyncio
async def test_docker_runner_start_applies_sandbox_flags(monkeypatch, tmp_path):
    runner = DockerRunner(Settings(runner="docker", workspace=tmp_path, allow_network=False))
    seen = {}

    async def fake_exec(argv, cmd, timeout):
        seen["argv"] = argv
        return CommandResult(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_exec", fake_exec)
    await runner.start()
    assert "--cap-drop=ALL" in seen["argv"]
    assert "--security-opt=no-new-privileges:true" in seen["argv"]
    assert "--network" in seen["argv"] and "none" in seen["argv"]


@pytest.mark.asyncio
async def test_docker_tool_exact_image_allowlist_and_digest_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(tx.shutil, "which", lambda _name: "/usr/bin/docker")
    settings = Settings(workspace=tmp_path, allow_network=False, agents_can_shell=True, command_timeout=5)
    called = False

    async def fake_exec(argv, cmd, timeout):
        nonlocal called
        called = True
        return CommandResult(cmd, 0, "ok", "")

    ctx = type("Ctx", (), {"core": type("Core", (), {
        "settings": settings, "runner": type("Runner", (), {"_exec": staticmethod(fake_exec)})()
    })()})()
    monkeypatch.setenv("AVRONE_DOCKER_IMAGE_ALLOWLIST", "python:3.12-slim")
    denied = await tx.t_docker_run(ctx, {"image": "alpine:latest", "cmd": "id"})
    assert "not in AVRONE_DOCKER_IMAGE_ALLOWLIST" in denied
    assert not called

    monkeypatch.delenv("AVRONE_DOCKER_IMAGE_ALLOWLIST", raising=False)
    monkeypatch.setenv("AVRONE_DOCKER_REQUIRE_DIGEST", "true")
    denied = await tx.t_docker_run(ctx, {"image": "python:3.12-slim", "cmd": "id"})
    assert "pinned by sha256 digest" in denied
    assert not called
