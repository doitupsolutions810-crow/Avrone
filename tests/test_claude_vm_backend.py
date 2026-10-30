from __future__ import annotations

import json
import pytest

from avrone.claude_agent_node import ClaudeVMLease
from avrone.claude_vm_backend import ClaudeSDKVMBackend
from avrone.disposable_vm_runner import DisposableVMRunner, VMRunnerError


class Provider:
    def __init__(self, *, wrong_worker=False, exit_code=0):
        self.events = []
        self.wrong_worker = wrong_worker
        self.exit_code = exit_code
        self.request = None
        self.env = None

    async def provision(self, task):
        self.events.append("provision")
        return ClaudeVMLease(
            lease_id="vm-lease-1", worker_id="worker-1", workspace="/workspace",
            disposable_vm=True, network_egress_denied=True,
        )

    async def run_guest(self, lease, *, argv, environment, stdin, timeout_seconds):
        self.events.append("execute")
        self.request = json.loads(stdin)
        self.env = dict(environment)
        assert argv == ["python", "-m", "avrone.claude_agent_node"]
        assert timeout_seconds == 30
        worker_id = "other-worker" if self.wrong_worker else lease.worker_id
        result = {
            "status": "completed", "worker_id": worker_id, "lease_id": lease.lease_id,
            "session_id": "sdk-session-1", "summary": "SDK changes ready",
            "num_turns": 2, "cost_usd": 0.04, "sdk_version": "0.2.175",
        }
        return {
            "exit_code": self.exit_code,
            "stdout": json.dumps({"ok": True, "result": result}),
        }

    async def collect(self, lease, result):
        self.events.append("collect")
        return {
            "status": "completed",
            "worker_id": lease.worker_id,
            "artifact_digests": [],
            "artifacts": [],
        }

    async def destroy(self, lease):
        self.events.append("destroy")


def task():
    return {
        "schema_version": 1,
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "source": {"repository": "owner/repo", "commit_sha": "a" * 40},
        "resource_limits": {"timeout_seconds": 30, "memory_mb": 256, "output_bytes": 4096},
    }


@pytest.mark.asyncio
async def test_sdk_backend_plugs_into_disposable_vm_runner_and_collects(tmp_path):
    provider = Provider()
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "Implement the requested, reviewed patch.",
        enabled=True,
    )
    runner = DisposableVMRunner(backend)
    result = await runner(task())

    assert provider.events == ["provision", "execute", "collect", "destroy"]
    assert provider.request["objective"] == "Implement the requested, reviewed patch."
    assert provider.env["AVRONE_VM_DISPOSABLE"] == "1"
    assert provider.env["AVRONE_VM_NETWORK_EGRESS_DENIED"] == "1"
    assert provider.env["AVRONE_VM_DOCKER_SOCKET_MOUNTED"] == "0"
    assert provider.env["AVRONE_VM_GITHUB_CREDENTIALS_MOUNTED"] == "0"
    assert result["worker_id"] == "worker-1"
    assert result["agent_session_id"] == "sdk-session-1"
    assert result["agent_summary"] == "SDK changes ready"
    assert result["artifact_digests"] == []


@pytest.mark.asyncio
async def test_disabled_sdk_backend_does_not_allocate_vm():
    provider = Provider()
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "Not run.", enabled=False,
    )
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="disabled"):
        await runner(task())
    assert provider.events == []


@pytest.mark.asyncio
async def test_wrong_worker_identity_never_collects_or_succeeds():
    provider = Provider(wrong_worker=True)
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "Test identity binding.",
        enabled=True,
    )
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="identity"):
        await runner(task())
    assert provider.events == ["provision", "execute", "destroy"]


@pytest.mark.asyncio
async def test_guest_failure_still_destroys_vm():
    provider = Provider(exit_code=2)
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "This fails.",
        enabled=True,
    )
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="non-zero"):
        await runner(task())
    assert provider.events == ["provision", "execute", "destroy"]


@pytest.mark.asyncio
async def test_collection_identity_mismatch_is_not_accepted():
    class BadCollectionProvider(Provider):
        async def collect(self, lease, result):
            self.events.append("collect")
            return {"status": "completed", "worker_id": "wrong"}

    provider = BadCollectionProvider()
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "No mismatched evidence.",
        enabled=True,
    )
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="identity"):
        await runner(task())
    assert provider.events == ["provision", "execute", "collect", "destroy"]


@pytest.mark.asyncio
async def test_boolean_exit_code_is_not_a_success_status():
    provider = Provider(exit_code=False)
    backend = ClaudeSDKVMBackend(
        provider, objective_resolver=lambda _task: "Do not treat booleans as process exit codes.",
        enabled=True,
    )
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="non-zero"):
        await runner(task())
    assert provider.events == ["provision", "execute", "destroy"]
