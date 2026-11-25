"""HTTP, TCP, and command health checks."""

from __future__ import annotations

import socket
import subprocess
import urllib.error
import urllib.request

from .spec import HealthSpec


def check(spec: HealthSpec) -> tuple[bool, str]:
    if spec.type == "process":
        return True, "process"
    if spec.type == "tcp":
        return _tcp(spec)
    if spec.type == "http":
        return _http(spec)
    if spec.type == "command":
        return _command(spec)
    return False, f"unknown health type {spec.type}"


def _tcp(spec: HealthSpec) -> tuple[bool, str]:
    try:
        with socket.create_connection((spec.host, spec.port), timeout=spec.timeout_seconds):
            return True, f"tcp {spec.host}:{spec.port}"
    except OSError as exc:
        return False, f"tcp {spec.host}:{spec.port}: {exc}"


def _http(spec: HealthSpec) -> tuple[bool, str]:
    request = urllib.request.Request(spec.url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=spec.timeout_seconds) as response:
            status = getattr(response, "status", None) or response.getcode()
            if int(status) == spec.expect_status:
                return True, f"http {status}"
            return False, f"http {status}, expected {spec.expect_status}"
    except urllib.error.HTTPError as exc:
        if exc.code == spec.expect_status:
            return True, f"http {exc.code}"
        return False, f"http {exc.code}, expected {spec.expect_status}"
    except Exception as exc:
        return False, f"http {spec.url}: {exc}"


def _command(spec: HealthSpec) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            spec.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=spec.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "health command timed out"
    except OSError as exc:
        return False, f"health command failed to start: {exc}"
    if completed.returncode == 0:
        return True, "command exit 0"
    detail = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace").strip()
    return False, f"command exit {completed.returncode}" + (f": {detail[:200]}" if detail else "")
