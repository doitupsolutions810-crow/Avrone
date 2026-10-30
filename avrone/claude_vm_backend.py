"""Adapter connecting AVRONE's DisposableVMRunner to the Claude SDK guest CLI.

The provider owns the real VM API and must enforce isolation. This adapter only
translates validated server task data into the guest contract; it is not itself
a VM implementation.
"""
from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .claude_agent_node import ClaudeVMLease
from .disposable_vm_runner import VMRunnerError

ObjectiveResolver = Callable[[Mapping[str, Any]], str | Awaitable[str]]


class ClaudeGuestVMProvider(Protocol):
    """Provider contract; implementations must operate a real disposable VM."""

    async def provision(self, task: Mapping[str, Any]) -> ClaudeVMLease: ...
    async def run_guest(
        self,
        lease: ClaudeVMLease,
        *,
        argv: list[str],
        environment: Mapping[str, str],
        stdin: bytes,
        timeout_seconds: int,
    ) -> Mapping[str, Any]: ...
    async def collect(
        self, lease: ClaudeVMLease, result: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    async def destroy(self, lease: ClaudeVMLease) -> None: ...


class ClaudeSDKVMBackend:
    """VMBackend implementation that runs the SDK entry point inside the VM."""

    def __init__(
        self,
        provider: ClaudeGuestVMProvider,
        *,
        objective_resolver: ObjectiveResolver,
        enabled: bool | None = None,
    ):
        self.provider = provider
        self.objective_resolver = objective_resolver
        self.enabled = (
            os.environ.get("AVRONE_CLAUDE_SDK_ENABLED", "0") == "1"
            if enabled is None else enabled is True
        )

    @staticmethod
    def _assert_provider_lease(lease: Any) -> ClaudeVMLease:
        if not isinstance(lease, ClaudeVMLease):
            raise VMRunnerError("VM provider returned an invalid Claude lease")
        if not lease.lease_id or not lease.worker_id:
            raise VMRunnerError("VM provider lease identity is missing")
        if lease.disposable_vm is not True or lease.network_egress_denied is not True:
            raise VMRunnerError("VM provider did not attest disposable, network-denied execution")
        if lease.docker_socket_mounted is not False or lease.github_credentials_mounted is not False:
            raise VMRunnerError("VM provider exposed a host socket or ambient GitHub credentials")
        if not isinstance(lease.workspace, str) or not lease.workspace.startswith("/"):
            raise VMRunnerError("VM provider workspace must be an absolute guest path")
        return lease

    async def provision(self, task: Mapping[str, Any]) -> ClaudeVMLease:
        if not self.enabled:
            raise VMRunnerError("Claude Agent SDK VM backend is disabled")
        lease = await self.provider.provision(task)
        return self._assert_provider_lease(lease)

    async def execute(
        self, lease: ClaudeVMLease, task: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        lease = self._assert_provider_lease(lease)
        if not self.enabled:
            raise VMRunnerError("Claude Agent SDK VM backend is disabled")
        try:
            objective = self.objective_resolver(task)
            if inspect.isawaitable(objective):
                objective = await objective
        except Exception as exc:
            raise VMRunnerError("trusted task objective could not be resolved") from exc
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 16_000:
            raise VMRunnerError("trusted task objective is empty or exceeds 16 KiB")

        source = task.get("source")
        limits = task.get("resource_limits")
        if not isinstance(source, Mapping) or not isinstance(limits, Mapping):
            raise VMRunnerError("validated task source or resource limits are missing")
        request = {
            "task_id": task.get("task_id"),
            "correlation_id": task.get("correlation_id"),
            "objective": objective,
            "source_repository": source.get("repository"),
            "source_commit_sha": source.get("commit_sha"),
        }
        try:
            encoded = json.dumps(
                request, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise VMRunnerError("guest work request is not valid JSON") from exc
        if len(encoded) > 24 * 1024:
            raise VMRunnerError("guest work request exceeds 24 KiB")

        timeout = limits.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise VMRunnerError("task timeout is invalid")
        timeout = min(timeout, 900)
        environment = {
            "AVRONE_CLAUDE_SDK_ENABLED": "1",
            "AVRONE_VM_LEASE_ID": lease.lease_id,
            "AVRONE_VM_WORKER_ID": lease.worker_id,
            "AVRONE_VM_WORKSPACE": lease.workspace,
            "AVRONE_VM_DISPOSABLE": "1",
            "AVRONE_VM_NETWORK_EGRESS_DENIED": "1",
            "AVRONE_VM_DOCKER_SOCKET_MOUNTED": "0",
            "AVRONE_VM_GITHUB_CREDENTIALS_MOUNTED": "0",
        }
        try:
            guest = await self.provider.run_guest(
                lease,
                argv=["python", "-m", "avrone.claude_agent_node"],
                environment=environment,
                stdin=encoded,
                timeout_seconds=timeout,
            )
        except Exception as exc:
            raise VMRunnerError("Claude SDK guest invocation failed") from exc
        if not isinstance(guest, Mapping) or guest.get("exit_code") != 0:
            raise VMRunnerError("Claude SDK guest returned a non-zero exit status")
        stdout = guest.get("stdout")
        if isinstance(stdout, bytes):
            if len(stdout) > 64 * 1024:
                raise VMRunnerError("Claude SDK guest output exceeded 64 KiB")
            try:
                stdout = stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VMRunnerError("Claude SDK guest output is not UTF-8") from exc
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > 64 * 1024:
            raise VMRunnerError("Claude SDK guest output is missing or oversized")
        try:
            payload = json.loads(stdout)
        except ValueError as exc:
            raise VMRunnerError("Claude SDK guest output is invalid JSON") from exc
        result = payload.get("result") if isinstance(payload, dict) and payload.get("ok") is True else None
        if (
            not isinstance(result, dict)
            or result.get("status") != "completed"
            or result.get("worker_id") != lease.worker_id
            or result.get("lease_id") != lease.lease_id
            or not isinstance(result.get("session_id"), str)
            or not result.get("session_id")
            or not isinstance(result.get("summary"), str)
        ):
            raise VMRunnerError("Claude SDK guest result failed identity or completion checks")
        return {
            "status": "completed",
            "worker_id": lease.worker_id,
            "agent_result": {
                "session_id": result["session_id"],
                "summary": result["summary"][:12_000],
                "num_turns": result.get("num_turns"),
                "cost_usd": result.get("cost_usd"),
                "sdk_version": result.get("sdk_version"),
            },
        }

    async def collect(
        self, lease: ClaudeVMLease, result: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        collected = await self.provider.collect(lease, result)
        if not isinstance(collected, Mapping) or collected.get("status") != "completed":
            raise VMRunnerError("VM provider artifact collection failed")
        if collected.get("worker_id") != lease.worker_id:
            raise VMRunnerError("collected artifact worker identity does not match VM lease")
        agent_result = result.get("agent_result")
        if not isinstance(agent_result, Mapping):
            raise VMRunnerError("Claude SDK worker summary is missing")
        output = dict(collected)
        output["agent_summary"] = str(agent_result.get("summary", ""))[:12_000]
        output["agent_session_id"] = str(agent_result.get("session_id", ""))
        if isinstance(agent_result.get("cost_usd"), (int, float)) and not isinstance(agent_result.get("cost_usd"), bool):
            output["agent_cost_usd"] = float(agent_result["cost_usd"])
        return output

    async def destroy(self, lease: ClaudeVMLease) -> None:
        await self.provider.destroy(lease)
