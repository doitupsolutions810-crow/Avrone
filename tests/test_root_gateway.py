import pytest

import avrone.root_gateway as gateway


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"active\n", b""

    async def wait(self):
        return None

    def kill(self):
        return None


def configure(monkeypatch):
    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_TOKEN", "g" * 48)
    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_OPERATIONS", "service_status,service_restart")
    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_SERVICES", "avrone.service,nginx.service")


@pytest.mark.asyncio
async def test_gateway_requires_explicit_enable_and_secret(monkeypatch):
    monkeypatch.delenv("AVRONE_ROOT_GATEWAY_ENABLED", raising=False)
    with pytest.raises(PermissionError, match="disabled"):
        await gateway.dispatch_root_operation("service_status", "avrone.service", "g" * 48)

    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AVRONE_ROOT_GATEWAY_TOKEN", "short")
    with pytest.raises(PermissionError, match="at least 32"):
        await gateway.dispatch_root_operation("service_status", "avrone.service", "short")


@pytest.mark.asyncio
async def test_gateway_checks_token_operation_and_service(monkeypatch):
    configure(monkeypatch)
    with pytest.raises(PermissionError, match="invalid"):
        await gateway.dispatch_root_operation("service_status", "avrone.service", "wrong")
    with pytest.raises(PermissionError, match="not allowlisted"):
        await gateway.dispatch_root_operation("system_shell", "avrone.service", "g" * 48)
    with pytest.raises(PermissionError, match="not allowlisted"):
        await gateway.dispatch_root_operation("service_restart", "sshd.service", "g" * 48)
    with pytest.raises(ValueError, match="valid systemd unit"):
        await gateway.dispatch_root_operation("service_restart", "bad/name", "g" * 48)


@pytest.mark.asyncio
async def test_gateway_executes_only_allowlisted_argv_without_shell(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(gateway.shutil, "which", lambda name: {
        "systemctl": "/usr/bin/systemctl", "sudo": "/usr/bin/sudo"
    }.get(name))
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(gateway.asyncio, "create_subprocess_exec", fake_exec)
    result = await gateway.dispatch_root_operation("service_restart", "avrone.service", "g" * 48)
    assert seen["argv"] == (
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "avrone.service"
    )
    assert "shell" not in seen["kwargs"]
    assert "AVRONE_ROOT_GATEWAY_TOKEN" not in seen["kwargs"]["env"]
    assert result["exit_code"] == 0
    assert result["privileged"] is True
