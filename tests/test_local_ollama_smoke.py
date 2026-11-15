"""Offline tests for the live Ollama/Docker smoke-test verifier.

These tests fake Docker's read-only inspection commands. They do not claim to test a live
Docker daemon; the dedicated GitHub Actions smoke job exercises that separate boundary.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from avrone import agentcontainer as container_policy

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "local-ollama" / "test-local-ollama-squad.py"
IMAGE_REF = "python:3.12-slim"
IMAGE_ID = "sha256:" + "d" * 64
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
        "Image": IMAGE_ID,
        "Name": f"/avrone-agent-node-{agent_id}",
        "State": {"Running": True},
        "Config": {
            "Labels": {"avrone.agent-home": "1", "avrone.agent": agent_id},
            "Image": IMAGE_ID,
            "User": "501:20",
            "WorkingDir": "/agent",
            "Env": ["HOME=/agent", "TMPDIR=/tmp", "PYTHONDONTWRITEBYTECODE=1",
                    "AVRONE_AGENT_DIR=/agent"],
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
            "AutoRemove": False,
            "Devices": [],
            "DeviceRequests": [],
            "DeviceCgroupRules": [],
            "Runtime": "runc",
            "UsernsMode": "",
            "Sysctls": {},
            "GroupAdd": [],
            "PidMode": "",
            "IpcMode": "",
            "Memory": container_policy.mem_mb() * 1024 * 1024,
            "MemorySwap": container_policy.mem_mb() * 1024 * 1024,
            "NanoCpus": int(round(container_policy.cpus() * 1_000_000_000)),
            "PidsLimit": container_policy.PIDS_LIMIT,
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
            "Binds": [f"{source}:/agent"],
        },
        "Mounts": [{
            "Type": "bind", "Source": source, "Destination": "/agent", "RW": True,
        }],
        "_proof": {"content": content, "sha256": digest, "marker": marker},
    }


def _install_docker_stub(monkeypatch, containers):
    monkeypatch.setenv("AVRONE_WORKSPACE", "/workspace")
    container_map = {c["Id"]: c for c in containers}
    def fake_run(args, check=False, capture_output=False, text=False, timeout=None):
        if args[:3] == ["docker", "image", "inspect"]:
            assert args[3:5] == ["--format", "{{.Id}}"]
            assert args[5] == os.environ.get("AVRONE_DOCKER_IMAGE", IMAGE_REF)
            first = next(iter(container_map.values()), {})
            if first.get("_image_inspect_error"):
                raise subprocess.CalledProcessError(1, args, stderr="image missing")
            image_id = first.get("_resolved_image_id", IMAGE_ID)
            return subprocess.CompletedProcess(args, 0, f"{image_id}\n", "")
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(container_map), "")
        if args[:2] == ["docker", "inspect"]:
            payload = []
            for cid in args[2:]:
                item = dict(container_map[cid])
                item["Id"] = container_map[cid].get("_reported_inspect_id", item["Id"])
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
        _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        _container("solver-b", "worker-B proof\\n", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
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
    ("extra_mount", "exactly one private mount"),
    ("root_user", "isolation/artifact checks"),
])
def test_smoke_verifier_rejects_tampered_or_unhardened_worker_evidence(
    monkeypatch, mutation, expected_error
):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        _container("solver-b", "worker-B proof\\n", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
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



def test_smoke_verifier_accepts_explicit_readwrite_bind_suffix(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "a" * 64),
        _container("solver-b", "worker-B proof\\n", "b" * 64),
    ]
    for item in containers:
        item["HostConfig"]["Binds"] = [item["HostConfig"]["Binds"][0] + ":rw"]
    _install_docker_stub(monkeypatch, containers)

    result = smoke.inspect_agent_homes("nonce", _expected(containers))
    assert len(result) == 2
    assert all(all(item["checks"].values()) for item in result)


def test_smoke_verifier_rejects_missing_container_for_a_shell_caller(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ]
    _install_docker_stub(monkeypatch, containers)
    expected = _expected(containers)
    expected["solver-b"] = {"content": "missing\\n", "sha256": "0" * 64}
    with pytest.raises(RuntimeError, match="do not match the two shell callers"):
        smoke.inspect_agent_homes("nonce", expected)


def test_smoke_verifier_rejects_malformed_docker_inspect_shape(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [_container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    expected = _expected(containers)
    def malformed(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(args)
    monkeypatch.setattr(smoke.subprocess, "run", malformed)
    with pytest.raises((TypeError, RuntimeError, AttributeError)):
        smoke.inspect_agent_homes("nonce", expected)

@pytest.mark.parametrize("mutation,expected_error", [
    ("wrong_image", "expected_image"),
    ("wrong_actual_image", "actual_image_id_matches"),
    ("nnp_false", "no_new_privileges"),
    ("extra_tmpfs", "bounded_tmpfs"),
    ("tmpfs_lookalike", "bounded_tmpfs"),
    ("tmpfs_extra_option", "bounded_tmpfs"),
    ("auto_remove", "auto_remove_disabled"),
    ("wrong_memory", "memory_limit_exact"),
    ("wrong_cpu", "cpu_limit_exact"),
    ("wrong_pids", "pids_limit_exact"),
    ("missing_ulimit", "open_file_limit_exact"),
    ("widened_ulimit", "open_file_limit_exact"),
    ("duplicate_ulimit", "open_file_limit_exact"),
    ("boolean_ulimit", "open_file_limit_exact"),
    ("extra_ulimit", "open_file_limit_exact"),
    ("duplicate_home_env", "required_environment_exact"),
    ("wrong_bind", "one_private_mount"),
    ("wrong_bind_readonly", "one_private_mount"),
    ("wrong_bind_relabel", "one_private_mount"),
    ("wrong_workspace_root", "one_private_mount"),
])
def test_smoke_verifier_rejects_resource_and_environment_lookalikes(monkeypatch, mutation, expected_error):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        _container("solver-b", "worker-B proof\\n", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ]
    host, cfg = containers[0]["HostConfig"], containers[0]["Config"]
    if mutation == "wrong_image":
        cfg["Image"] = "attacker:latest"
    elif mutation == "wrong_actual_image":
        containers[0]["Image"] = "sha256:" + "0" * 64
    elif mutation == "nnp_false":
        host["SecurityOpt"] = ["no-new-privileges=false"]
    elif mutation == "extra_tmpfs":
        host["Tmpfs"]["/run"] = "rw,size=1g"
    elif mutation == "tmpfs_lookalike":
        host["Tmpfs"]["/tmp"] = "notrw,nosuid,nodev,size=128m"
    elif mutation == "auto_remove":
        host["AutoRemove"] = True
    elif mutation == "wrong_memory":
        host["Memory"] += 1
    elif mutation == "wrong_cpu":
        host["NanoCpus"] = 1
    elif mutation == "wrong_pids":
        host["PidsLimit"] = 1
    elif mutation == "missing_ulimit":
        host.pop("Ulimits", None)
    elif mutation == "widened_ulimit":
        host["Ulimits"][0]["Hard"] = 65536
    elif mutation == "duplicate_ulimit":
        host["Ulimits"].append({"Name": "nofile", "Soft": 1, "Hard": 1})
    elif mutation == "boolean_ulimit":
        host["Ulimits"][0]["Soft"] = True
    elif mutation == "extra_ulimit":
        host["Ulimits"][0]["Extra"] = "unexpected"
    elif mutation == "duplicate_home_env":
        cfg["Env"].append("HOME=/tmp")
    elif mutation == "wrong_bind":
        host["Binds"] = ["/etc:/agent"]
    elif mutation == "wrong_bind_readonly":
        host["Binds"] = [host["Binds"][0] + ":ro"]
    elif mutation == "wrong_bind_relabel":
        host["Binds"] = [host["Binds"][0] + ":z"]
    elif mutation == "wrong_workspace_root":
        host["Binds"] = ["/another/agents/solver-a:/agent"]
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match=expected_error):
        smoke.inspect_agent_homes("nonce", _expected(containers))



@pytest.mark.parametrize("bad_id", [
    "python:3.12-slim",
    "",
    "sha256:" + "g" * 64,
    "sha256:" + "0" * 63,
])
def test_smoke_verifier_rejects_non_immutable_or_malformed_image_ids(monkeypatch, bad_id):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "a" * 64),
        _container("solver-b", "worker-B proof\\n", "b" * 64),
    ]
    containers[0]["_resolved_image_id"] = bad_id
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match="malformed immutable image ID"):
        smoke.inspect_agent_homes("nonce", _expected(containers))


def test_smoke_verifier_fails_closed_when_image_resolution_fails(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "a" * 64),
        _container("solver-b", "worker-B proof\\n", "b" * 64),
    ]
    containers[0]["_image_inspect_error"] = True
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match="Unable to resolve the configured Docker image"):
        smoke.inspect_agent_homes("nonce", _expected(containers))


def test_smoke_verifier_rejects_inspect_identity_not_in_listed_inventory(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "a" * 64),
        _container("solver-b", "worker-B proof\\n", "b" * 64),
    ]
    containers[0]["_reported_inspect_id"] = "c" * 64
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match="identities do not match"):
        smoke.inspect_agent_homes("nonce", _expected(containers))


def test_smoke_verifier_rejects_duplicate_container_claims_for_one_worker(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    first = _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    duplicate = _container("solver-a", "worker-A proof\\n", "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc")
    other = _container("solver-b", "worker-B proof\\n", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    _install_docker_stub(monkeypatch, [first, duplicate, other])
    with pytest.raises(RuntimeError, match="Multiple Docker containers"):
        smoke.inspect_agent_homes("nonce", _expected([first, other]))


def test_smoke_verifier_rejects_invalid_inspect_json_cleanly(monkeypatch):
    def malformed(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "not-json", "")
        raise AssertionError(args)
    monkeypatch.setattr(smoke.subprocess, "run", malformed)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        smoke.inspect_agent_homes("nonce", {"solver-a": {"content": "x\\n", "sha256": "0" * 64}})


def test_live_smoke_never_substitutes_worker_role_for_missing_durable_id():
    with pytest.raises(RuntimeError, match="canonical durable agent_id"):
        smoke._durable_spawn_ids([
            {"agent": "Worker A", "data": {"role": "Worker A"}},
            {"agent": "Worker B", "data": {"role": "Worker B"}},
        ])


def test_live_smoke_rejects_duplicate_durable_worker_ids():
    with pytest.raises(RuntimeError, match="share one durable"):
        smoke._durable_spawn_ids([
            {"agent": "Worker A", "data": {"agent_id": "solver-a"}},
            {"agent": "Worker B", "data": {"agent_id": "solver-a"}},
        ])

@pytest.mark.parametrize("mutation,expected_check", [
    ("seccomp_unconfined", "no_security_profile_overrides"),
    ("apparmor_unconfined", "no_security_profile_overrides"),
    ("host_uts", "private_uts_namespace"),
    ("host_cgroup", "private_cgroup_namespace"),
    ("volumes_from", "no_volumes_from"),
    ("secret_env", "required_environment_exact"),
    ("duplicate_env", "required_environment_exact"),
    ("malformed_pid_namespace", "private_pid_namespace"),
    ("malformed_ipc_namespace", "private_ipc_namespace"),
    ("malformed_uts_namespace", "private_uts_namespace"),
    ("malformed_cgroup_namespace", "private_cgroup_namespace"),
    ("custom_runtime", "default_runtime_only"),
    ("host_userns", "no_userns_override"),
    ("sysctl_override", "no_sysctl_overrides"),
    ("supplemental_group", "no_supplemental_groups"),
    ("device_cgroup_rule", "no_device_access"),
])
def test_smoke_verifier_rejects_additional_security_and_namespace_escapes(
    monkeypatch, mutation, expected_check
):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    containers = [
        _container("solver-a", "worker-A proof\\n", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        _container("solver-b", "worker-B proof\\n", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ]
    host = containers[0]["HostConfig"]
    if mutation == "seccomp_unconfined":
        host["SecurityOpt"] = ["no-new-privileges", "seccomp=unconfined"]
    elif mutation == "apparmor_unconfined":
        host["SecurityOpt"] = ["no-new-privileges", "apparmor=unconfined"]
    elif mutation == "host_uts":
        host["UTSMode"] = "host"
    elif mutation == "host_cgroup":
        host["CgroupnsMode"] = "host"
    elif mutation == "volumes_from":
        host["VolumesFrom"] = ["other-container"]
    elif mutation == "secret_env":
        containers[0]["Config"]["Env"].append("AVRONE_ADMIN_TOKEN=leaked")
    elif mutation == "duplicate_env":
        containers[0]["Config"]["Env"].append("HOME=/tmp")
    elif mutation == "malformed_pid_namespace":
        host["PidMode"] = "shareable"
    elif mutation == "malformed_ipc_namespace":
        host["IpcMode"] = "container:another"
    elif mutation == "malformed_uts_namespace":
        host["UTSMode"] = {"mode": "private"}
    elif mutation == "malformed_cgroup_namespace":
        host["CgroupnsMode"] = "host-extra"
    elif mutation == "custom_runtime":
        host["Runtime"] = "nvidia"
    elif mutation == "host_userns":
        host["UsernsMode"] = "host"
    elif mutation == "sysctl_override":
        host["Sysctls"] = {"net.ipv4.ip_forward": "1"}
    elif mutation == "supplemental_group":
        host["GroupAdd"] = ["0"]
    elif mutation == "device_cgroup_rule":
        host["DeviceCgroupRules"] = ["c 1:1 rwm"]
    _install_docker_stub(monkeypatch, containers)
    with pytest.raises(RuntimeError, match=expected_check):
        smoke.inspect_agent_homes("nonce", _expected(containers))


def test_smoke_verifier_rejects_malformed_docker_ids(monkeypatch):
    def malformed_ids(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "--help\\n", "")
        raise AssertionError(f"docker inspect must not run for invalid IDs: {args!r}")
    monkeypatch.setattr(smoke.subprocess, "run", malformed_ids)
    with pytest.raises(RuntimeError, match="malformed container ID"):
        smoke.inspect_agent_homes("nonce", {"solver-a": {"content": "x\\n", "sha256": "0" * 64}})


def test_live_smoke_prompt_uses_machine_enforced_shell_contract():
    cmd = "printf proof > marker.txt && pwd && sha256sum -- marker.txt"
    task = smoke.build_smoke_task(cmd)
    assert "exactly two independent workers" in task
    assert "machine-readable AVRONE action contract" in task
    assert "Do not call think or any preparatory/non-shell tool" in task
    assert task.count(cmd) == 1
    contract_line = next(line for line in task.splitlines() if line.startswith("AVRONE_ACTION_CONTRACT:"))
    contract = json.loads(contract_line.split(":", 1)[1])
    assert contract == {
        "first_action": {"tool": "shell", "args": {"cmd": cmd}},
        "allow_followup_calls": False,
    }


def test_worker_prompt_gives_explicit_tool_actions_priority_over_setup_habits():
    from avrone.trinity import WORKER_PROMPT
    assert "overrides every general workflow habit" in WORKER_PROMPT
    assert "Do not call" in WORKER_PROMPT and "skill_load" in WORKER_PROMPT
    assert "perform that action first" in WORKER_PROMPT


def test_live_smoke_requires_exactly_two_shell_calls_and_results():
    cmd = "printf proof > marker.txt && pwd && sha256sum -- marker.txt"
    spawns = [
        {"agent": "WorkerA-1", "data": {"agent_id": "worker-a"}},
        {"agent": "WorkerB-2", "data": {"agent_id": "worker-b"}},
    ]
    calls = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "args": {"cmd": cmd}}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "args": {"cmd": cmd}}},
    ]
    results = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "output": "sha256 output A"}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "output": "sha256 output B"}},
    ]
    identities, outputs = smoke._validate_worker_tool_events(spawns, calls, results, cmd)
    assert identities == {"WorkerA-1": "worker-a", "WorkerB-2": "worker-b"}
    assert set(outputs) == {"WorkerA-1", "WorkerB-2"}


@pytest.mark.parametrize("mutation,expected_error", [
    ("extra_call", "exactly two total tool.call"),
    ("preparatory_call", "exactly two total tool.call"),
    ("wrong_tool", "only shell is permitted"),
    ("think_tool", "only shell is permitted"),
    ("wrong_command", "exact approved shell command"),
    ("unspawned_caller", "does not belong"),
    ("missing_result", "exactly two total tool.result"),
    ("duplicate_result", "exactly two total tool.result"),
    ("unspawned_result", "does not belong"),
])
def test_live_smoke_rejects_extra_or_mismatched_tool_events(mutation, expected_error):
    cmd = "printf proof > marker.txt && pwd && sha256sum -- marker.txt"
    spawns = [
        {"agent": "WorkerA-1", "data": {"agent_id": "worker-a"}},
        {"agent": "WorkerB-2", "data": {"agent_id": "worker-b"}},
    ]
    calls = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "args": {"cmd": cmd}}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "args": {"cmd": cmd}}},
    ]
    results = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "output": "sha256 output A"}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "output": "sha256 output B"}},
    ]
    if mutation in {"extra_call", "preparatory_call"}:
        tool = "skill_list" if mutation == "preparatory_call" else "shell"
        calls.append({"agent": "WorkerA-1", "data": {"tool": tool, "args": {"cmd": cmd}}})
    elif mutation in {"wrong_tool", "think_tool"}:
        calls[0]["data"]["tool"] = "think" if mutation == "think_tool" else "skill_list"
    elif mutation == "wrong_command":
        calls[0]["data"]["args"]["cmd"] = "echo fabricated"
    elif mutation == "unspawned_caller":
        calls[0]["agent"] = "Lead-1"
    elif mutation == "missing_result":
        results.pop()
    elif mutation == "duplicate_result":
        results.append(dict(results[0]))
    elif mutation == "unspawned_result":
        results[0]["agent"] = "Lead-1"
    with pytest.raises(RuntimeError, match=expected_error):
        smoke._validate_worker_tool_events(spawns, calls, results, cmd)

@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8710/api/status",
    "http://example.com/api/status",
    "http://localhost.attacker.test/api/status",
    "http://user@127.0.0.1:8710/api/status",
    "http://127.0.0.1:99999/api/status",
    "http://localhost:/api/status",
    "http://127.0.0.1:8710/api/status?next=https://example.com",
    "http://127.0.0.1:8710/api/status#fragment",
    "file:///etc/passwd",
    "http://[::1",
])
def test_live_smoke_rejects_non_loopback_or_ambiguous_api_urls(url):
    assert smoke._is_loopback_http_url(url) is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8710/api/status",
    "http://127.4.5.6:8710/api/status",
    "http://localhost:8710/api/status",
    "http://[::1]:8710/api/status",
])
def test_live_smoke_accepts_loopback_api_urls(url):
    assert smoke._is_loopback_http_url(url) is True


def test_request_json_refuses_redirects_even_from_loopback(monkeypatch):
    class RedirectingOpener:
        def open(self, request, timeout):
            raise smoke.urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://example.com/"}, None
            )

    def fake_build_opener(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], smoke._NoRedirect)
        return RedirectingOpener()

    monkeypatch.setattr(smoke.urllib.request, "build_opener", fake_build_opener)
    with pytest.raises(RuntimeError, match="redirect"):
        smoke.request_json("http://127.0.0.1:8710/api/status")


def test_request_json_converts_malformed_json_to_bounded_runtime_error(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return b"{not-json"

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 5
            return Response()

    monkeypatch.setattr(smoke.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError, match="malformed JSON"):
        smoke.request_json("http://localhost:8710/api/status", timeout=5)


def test_request_json_rejects_oversized_local_api_responses(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, limit):
            return b"x" * limit

    class Opener:
        def open(self, _request, timeout):
            return Response()

    monkeypatch.setattr(smoke.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError, match="exceeded"):
        smoke.request_json("http://127.0.0.1:8710/api/status")


def test_live_smoke_rejects_non_object_status_before_field_access(monkeypatch):
    monkeypatch.setattr(smoke, "request_json", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(smoke.sys, "argv", [
        "test-local-ollama-squad.py", "--profile", str(ROOT / "missing-profile.env")
    ])
    with pytest.raises(RuntimeError, match="status response must be a JSON object"):
        smoke.main()


def test_live_smoke_rejects_non_object_chat_response(monkeypatch):
    monkeypatch.setenv("AVRONE_ALLOW_NETWORK", "false")
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    responses = iter([
        {"provider": "ollama", "runner": "docker"},
        [],
    ])
    monkeypatch.setattr(smoke, "request_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(smoke.sys, "argv", [
        "test-local-ollama-squad.py", "--profile", str(ROOT / "missing-profile.env")
    ])
    with pytest.raises(RuntimeError, match="chat response must be a JSON object"):
        smoke.main()


@pytest.mark.parametrize("data", [None, "not-an-object", 7, [], ["unexpected"]])
def test_event_diagnostic_data_accessor_never_assumes_dict_shape(data):
    assert smoke._event_data({"data": data}) == {}

def test_local_mcp_credentials_and_sandbox_profile_are_git_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "mcp.json" in ignore
    assert "docs/local-ollama/local-ollama-sandbox.env" in ignore

@pytest.mark.parametrize("bad_agent", [None, 7, [], {}])
def test_live_smoke_rejects_non_string_tool_call_agent_identities(bad_agent):
    cmd = "printf proof > marker.txt && pwd && sha256sum -- marker.txt"
    spawns = [
        {"agent": "WorkerA-1", "data": {"agent_id": "worker-a"}},
        {"agent": "WorkerB-2", "data": {"agent_id": "worker-b"}},
    ]
    calls = [
        {"agent": bad_agent, "data": {"tool": "shell", "args": {"cmd": cmd}}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "args": {"cmd": cmd}}},
    ]
    results = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "output": "sha256 output A"}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "output": "sha256 output B"}},
    ]
    with pytest.raises(RuntimeError, match="does not belong"):
        smoke._validate_worker_tool_events(spawns, calls, results, cmd)


@pytest.mark.parametrize("bad_agent", [None, 7, [], {}])
def test_live_smoke_rejects_non_string_tool_result_agent_identities(bad_agent):
    cmd = "printf proof > marker.txt && pwd && sha256sum -- marker.txt"
    spawns = [
        {"agent": "WorkerA-1", "data": {"agent_id": "worker-a"}},
        {"agent": "WorkerB-2", "data": {"agent_id": "worker-b"}},
    ]
    calls = [
        {"agent": "WorkerA-1", "data": {"tool": "shell", "args": {"cmd": cmd}}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "args": {"cmd": cmd}}},
    ]
    results = [
        {"agent": bad_agent, "data": {"tool": "shell", "output": "sha256 output A"}},
        {"agent": "WorkerB-2", "data": {"tool": "shell", "output": "sha256 output B"}},
    ]
    with pytest.raises(RuntimeError, match="does not belong"):
        smoke._validate_worker_tool_events(spawns, calls, results, cmd)


@pytest.mark.parametrize("bad_agent", [None, 7, [], {}])
def test_event_agent_accessor_never_returns_non_string_identities(bad_agent):
    assert smoke._event_agent({"agent": bad_agent}) is None


def test_event_agent_accessor_preserves_non_empty_string_identity():
    assert smoke._event_agent({"agent": "WorkerA-1"}) == "WorkerA-1"

@pytest.mark.parametrize("body", [
    b'{"provider":"ollama","provider":"openai"}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":-Infinity}',
])
def test_request_json_rejects_duplicate_keys_and_nonstandard_json_constants(monkeypatch, body):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return body

    class Opener:
        def open(self, _request, timeout):
            return Response()

    monkeypatch.setattr(smoke.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError, match="malformed JSON"):
        smoke.request_json("http://127.0.0.1:8710/api/status")


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), 10**10000])
def test_request_json_rejects_invalid_timeouts_before_network(monkeypatch, timeout):
    monkeypatch.setattr(
        smoke.urllib.request, "build_opener",
        lambda *_args: pytest.fail("network opener must not be constructed for an invalid timeout"),
    )
    with pytest.raises(RuntimeError, match="positive finite"):
        smoke.request_json("http://localhost:8710/api/status", timeout=timeout)


def test_smoke_verifier_bounds_persistent_container_inventory(monkeypatch):
    ids = [f"{value:064x}" for value in range(1, 514)]

    def fake_run(args, check=False, capture_output=False, text=False, timeout=None):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(ids), "")
        raise AssertionError("Docker inspect and image resolution must not run for oversized inventory.")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="more than 512"):
        smoke.inspect_agent_homes("nonce", {"solver-a": {"content": "x", "sha256": "0" * 64}})

