"""Per-job disposable VM lifecycle adapter contract.

No cloud provider is selected here. A concrete backend must provision a fresh
VM for each task, enforce its resource/network policy, stage artifacts, and
destroy the VM. The adapter guarantees teardown around every outcome.
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Protocol


class VMBackend(Protocol):
    async def provision(self, task: Mapping[str, Any]) -> Any: ...
    async def execute(self, lease: Any, task: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def collect(self, lease: Any, result: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def destroy(self, lease: Any) -> None: ...


class VMRunnerError(RuntimeError):
    pass


class DisposableVMRunner:
    """Runner adapter with a fresh lease per call and mandatory teardown."""

    def __init__(self, backend: VMBackend, *, provision_timeout: float = 60,
                 execution_timeout: float = 900, cleanup_timeout: float = 60):
        for value in (provision_timeout, execution_timeout, cleanup_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("VM lifecycle timeouts must be positive numbers")
        self.backend = backend
        self.provision_timeout = float(provision_timeout)
        self.execution_timeout = float(execution_timeout)
        self.cleanup_timeout = float(cleanup_timeout)

    async def __call__(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        lease = None
        primary_error: BaseException | None = None
        try:
            lease = await asyncio.wait_for(
                self.backend.provision(task), timeout=self.provision_timeout
            )
            if lease is None:
                raise VMRunnerError("backend returned no VM lease")
            result = await asyncio.wait_for(
                self.backend.execute(lease, task), timeout=self.execution_timeout
            )
            if not isinstance(result, Mapping) or result.get("status") != "completed":
                raise VMRunnerError("VM execution did not complete successfully")
            collected = await asyncio.wait_for(
                self.backend.collect(lease, result), timeout=self.execution_timeout
            )
            if not isinstance(collected, Mapping):
                raise VMRunnerError("VM backend returned an invalid collected result")
            if collected.get("status") != "completed":
                raise VMRunnerError("VM artifact collection did not complete")
            if not collected.get("worker_id"):
                raise VMRunnerError("VM result must identify the worker lease")
            return dict(collected)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if lease is not None:
                try:
                    cleanup = self.backend.destroy(lease)
                    await asyncio.wait_for(
                        asyncio.shield(cleanup), timeout=self.cleanup_timeout
                    )
                except BaseException as cleanup_error:
                    if primary_error is None:
                        raise VMRunnerError("VM teardown failed; job is not safe to mark complete") from cleanup_error


def require_production_vm_backend(backend: VMBackend, *, production: bool) -> None:
    """Refuse a missing backend in production; test fakes remain explicit."""
    if production and backend is None:
        raise VMRunnerError("production execution requires a disposable VM backend")
