import pytest

from avrone.config import Settings
from avrone.sovereign import LocalRunner, _agent_safe_environment


def test_agent_environment_removes_application_credentials():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/agent",
        "AVRONE_ADMIN_TOKEN": "admin-value",
        "AVRONE_API_TOKEN": "api-value",
        "AVRONE_FLEET_TOKEN": "fleet-value",
        "OPENAI_API_KEY": "provider-value",
        "GITHUB_TOKEN": "github-value",
        "LANG": "C.UTF-8",
    }
    safe = _agent_safe_environment(source)
    assert safe["PATH"] == "/usr/bin"
    assert safe["HOME"] == "/tmp/agent"
    assert safe["LANG"] == "C.UTF-8"
    assert not ((set(source) - {"PATH", "HOME", "LANG"}) & set(safe))


@pytest.mark.asyncio
async def test_local_runner_refuses_effective_root_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr("avrone.sovereign.os.geteuid", lambda: 0)
    monkeypatch.delenv("AVRONE_ALLOW_ROOT_LOCAL", raising=False)
    runner = LocalRunner(Settings(runner="local", workspace=tmp_path))
    with pytest.raises(RuntimeError, match="Refusing to run agent shell commands"):
        await runner.start()


@pytest.mark.asyncio
async def test_local_runner_root_override_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr("avrone.sovereign.os.geteuid", lambda: 0)
    monkeypatch.setenv("AVRONE_ALLOW_ROOT_LOCAL", "true")
    runner = LocalRunner(Settings(runner="local", workspace=tmp_path))
    await runner.start()
    assert tmp_path.is_dir()


@pytest.mark.asyncio
async def test_local_runner_refuses_to_claim_network_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr("avrone.sovereign.os.geteuid", lambda: 1000)
    runner = LocalRunner(Settings(runner="local", workspace=tmp_path, allow_network=False))
    with pytest.raises(RuntimeError, match="cannot enforce AVRONE_ALLOW_NETWORK=false"):
        await runner.start()
