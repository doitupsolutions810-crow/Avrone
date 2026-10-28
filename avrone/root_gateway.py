"""Operator-only, allowlisted host operations; deliberately no arbitrary command input."""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import shutil
import time
from typing import Any

from .sovereign import _agent_safe_environment

_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$")
_ALLOWED_OPERATIONS = frozenset({"service_status", "service_restart"})


def _csv_env(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


def _enabled() -> bool:
    return os.getenv("AVRONE_ROOT_GATEWAY_ENABLED", "").strip().lower() in {"1", "true", "yes", "allow"}


async def dispatch_root_operation(operation: object, service: object, token: str | None) -> dict[str, Any]:
    """Run one allowlisted systemd action after two-factor application-level authorization.

    This is not an agent tool and does not accept an arbitrary command, argv, path, or shell string.
    The host must separately grant non-interactive sudo for only the exact approved operations.
    """
    if not _enabled():
        raise PermissionError("root gateway is disabled (set AVRONE_ROOT_GATEWAY_ENABLED=true to opt in)")
    expected = os.getenv("AVRONE_ROOT_GATEWAY_TOKEN", "")
    if len(expected) < 32:
        raise PermissionError("root gateway token must be configured with at least 32 characters")
    if not isinstance(token, str) or not hmac.compare_digest(token, expected):
        raise PermissionError("invalid root gateway token")

    allowed = _csv_env("AVRONE_ROOT_GATEWAY_OPERATIONS")
    if operation not in _ALLOWED_OPERATIONS:
        raise ValueError("operation must be service_status or service_restart")
    if operation not in allowed:
        raise PermissionError(f"operation {operation!r} is not allowlisted on this node")
    if not isinstance(service, str) or not _SERVICE_RE.fullmatch(service):
        raise ValueError("service must be a valid systemd unit name")
    if service not in _csv_env("AVRONE_ROOT_GATEWAY_SERVICES"):
        raise PermissionError(f"service {service!r} is not allowlisted on this node")

    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is unavailable on this host")
    action = "is-active" if operation == "service_status" else "restart"
    argv = [systemctl, action, service]
    privileged = hasattr(os, "geteuid") and os.geteuid() != 0
    if privileged:
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError("sudo is unavailable; configure a restricted host policy or run the service as root")
        argv = [sudo, "-n", *argv]

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_agent_safe_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("root operation timed out after 15 seconds") from None

    return {
        "operation": operation,
        "service": service,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout.decode("utf-8", errors="replace")[:4_000],
        "stderr": stderr.decode("utf-8", errors="replace")[:2_000],
        "duration_s": round(time.monotonic() - started, 3),
        "privileged": privileged,
    }
