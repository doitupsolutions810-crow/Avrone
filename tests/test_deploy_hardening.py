"""Static checks that the deploy surface stays least-privilege. No Docker needed."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["avrone"]


def test_dockerfile_runs_as_non_root():
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.M)
    assert users and users[-1] not in ("root", "0", "0:0")
    # USER comes after the install steps and before CMD
    assert DOCKERFILE.index("pip install") < DOCKERFILE.rindex("USER ") < DOCKERFILE.index("CMD")


def test_dockerfile_network_off_and_healthcheck():
    assert "AVRONE_ALLOW_NETWORK=false" in DOCKERFILE
    assert re.search(r"^HEALTHCHECK .*", DOCKERFILE, re.M)
    assert "/api/status" in DOCKERFILE
    # same paths/ports as before (backward compatible)
    for kv in ("AVRONE_PORT=8710", "AVRONE_WORKSPACE=/srv/workspace", "AVRONE_DATA_DIR=/srv/data"):
        assert kv in DOCKERFILE
    assert 'CMD ["avrone"]' in DOCKERFILE and "EXPOSE 8710" in DOCKERFILE


def test_compose_least_privilege():
    svc = _compose()
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert not svc.get("privileged")
    assert str(svc["read_only"]).startswith("${AVRONE_READ_ONLY:-true}") or svc["read_only"] is True
    assert "/tmp" in svc["tmpfs"]
    assert svc["user"].startswith("${AVRONE_UID:-1000}")
    assert svc["init"] is True


def test_compose_defaults_locked_down_but_compatible():
    svc = _compose()
    env = svc["environment"]
    assert env["AVRONE_ALLOW_NETWORK"] == "${AVRONE_ALLOW_NETWORK:-false}"
    assert env["AVRONE_HOST"] == "0.0.0.0" and env["AVRONE_DATA_DIR"] == "/srv/data"
    assert svc["ports"] == ["${AVRONE_PUBLISH_ADDR:-127.0.0.1}:${AVRONE_PUBLISH_PORT:-8710}:8710"]
    # unchanged contract: build, env_file, volumes, host gateway
    assert svc["build"] == "." and svc["env_file"] == ".env"
    assert "./data:/srv/data" in svc["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in svc["volumes"]
    assert svc["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_dockerignore_keeps_secrets_out():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in lines and "data/" in lines and "workspace/" in lines
