"""Offline tests for the live Ollama/Docker smoke-test verifier.

These tests fake Docker's read-only inspection commands. They do not claim to test a live
Docker daemon; the dedicated GitHub Actions smoke job exercises that separate boundary.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "local-ollama" / "test-local-ollama-squad.py"
SPEC = importlib.util.spec_from_file_location("avrone_local_ollama_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _container(agent_id: str, content: str, cid: str) -> dict:
    marker = "sandbox-smoke-proof.txt"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source = f"/workspace/agents/{agent_id}"
    return {
        "Id": cid,
        "Name": f"/avrone-agent-node-{agent_id}",
        "State": {"Running": True},
        "Config": {
            "Labels": {"avrone.agent-home": "1", "avrone.agent": agent_id},
            "User": "501:20",
            "WorkingDir": "/agent",
            "Env": ["HOME=/agent"],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=128m"},
            "PortBindings": None,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "none",
            "Privileged": False,
            "Devices": [],
            "DeviceRequests": [],
            "PidMode": "",
            "IpcMode": "",
            "Memory": 512 * 1024 * 1024,
            "MemorySwap": 512 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 512,
            "Binds": [f"{source}:/agent"],
        },
        "Mounts": [{
            "Type": "bind", "Source": source, "Destination": "/agent", "RW": True,
        }],
        "_proof": {"content": content, "sha256": digest, "marker": marker},
    }


def _install_docker_stub(monkeypatch, containers):
    container_map = {c["Id"]: c for c in containers}
    def fake_run(args, check=False, capture_output=False, text=False, timeout=None):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(container_map), "")
        if args[:2] == ["docker", "inspect"]:
            payload = []
            for cid in args[2:]:
                item = dict(container_map[cid])
                item.pop("_proof", None)
                payload.append(item)
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[:2] == ["docker", "exec"]:
            cid, *command = args[2:]
            c = container_map[cid]
            proof = c["_proof"]
            if command[:2] == ["test", "-f"]:
                code = 0 if c.get("_file_exists", True) else 1
                return subprocess.CompletedProcess(args, code, "", "")
            if command[:3] == ["test", "!", "-L"]:
                code = 1 if c.get("_is_symlink", False) else 0
                return subprocess.CompletedProcess(args, code, "", "")
            if command[:2] == ["sha256sum", "--"]:
                digest = c.get("_reported_sha256", proof["sha256"])
                return subprocess.CompletedProcess(args, 0, f"{digest}  {command[2]}\\n", "")
            if command[:2] == ["cat", "--"]:
                value = c.get("_reported_content", proof["content"])
                return subprocess.CompletedProcess(args, 0, value, "")
            raise AssertionError(f"Unexpected docker exec command: {command!r}")
        raise AssertionError(f"Unexpected subprocess call: {args!r}")
    monkeypatch.setattr(smoke.subprocess, "run", fake_run)


def _expected(containers):
    return {
        c["Config"]["Labels"]["avrone.agent"]: {
            "content": c["_proof"]["content"],
            "sha256": c["_proof"]["sha256"],
        }
        for c in containers
    }


def test_smoke_verifier_accepts_two_distinct_private_agent_homes(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "cid-a"),
        _container("solver-b", "worker-B proof\\n", "cid-b"),
    ]
    _install_docker_stub(monkeypatch, containers)
    result = smoke.inspect_agent_homes("nonce", _expected(containers))
    assert {c["agent_id"] for c in result} == {"solver-a", "solver-b"}
    assert len(result) == 2
    assert all(all(c["checks"].values()) for c in result)


@pytest.mark.parametrize("mutation,expected_error", [
    ("wrong_hash", "hash mismatch"),
    ("wrong_content", "content mismatch"),
    ("missing_file", "absent or a symlink"),
    ("symlink", "absent or a symlink"),
    ("writable_root", "isolation/artifact checks"),
    ("network", "isolation/artifact checks"),
    ("extra_mount", "match the two shell callers"),
    ("root_user", "isolation/artifact checks"),
])
def test_smoke_verifier_rejects_tampered_or_unhardened_worker_evidence(
    monkeypatch, mutation, expected_error
):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "cid-a"),
        _container("solver-b", "worker-B proof\\n", "cid-b"),
    ]
    c = containers[0]
    if mutation == "wrong_hash":
        c["_reported_sha256"] = "0" * 64
    elif mutation == "wrong_content":
        c["_reported_content"] = "different contents\\n"
    elif mutation == "missing_file":
        c["_file_exists"] = False
    elif mutation == "symlink":
        c["_is_symlink"] = True
    elif mutation == "writable_root":
        c["HostConfig"]["ReadonlyRootfs"] = False
    elif mutation == "network":
        c["HostConfig"]["NetworkMode"] = "bridge"
    elif mutation == "extra_mount":
        c["Mounts"].append({"Type": "bind", "Source": "/tmp", "Destination": "/host", "RW": True})
    elif mutation == "root_user":
        c["Config"]["User"] = "0:0"
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match=expected_error):
        smoke.inspect_agent_homes("nonce", _expected(containers))


def test_smoke_verifier_rejects_missing_container_for_a_shell_caller(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "cid-a"),
    ]
    _install_docker_stub(monkeypatch, containers)
    expected = _expected(containers)
    expected["solver-b"] = {"content": "missing\\n", "sha256": "0" * 64}
    with pytest.raises(RuntimeError, match="do not match the two shell callers"):
        smoke.inspect_agent_homes("nonce", expected)


def test_smoke_verifier_rejects_malformed_docker_inspect_shape(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [_container("solver-a", "worker-A proof\\n", "cid-a")]
    expected = _expected(containers)
    def malformed(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "cid-a", "")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(args)
    monkeypatch.setattr(smoke.subprocess, "run", malformed)
    with pytest.raises((TypeError, RuntimeError, AttributeError)):
        smoke.inspect_agent_homes("nonce", expected)
