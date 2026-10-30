import asyncio
import pytest

from avrone.disposable_vm_runner import DisposableVMRunner, VMRunnerError


class FakeBackend:
    def __init__(self, *, execute_error=None, collect_error=None, destroy_error=None):
        self.events = []
        self.execute_error = execute_error
        self.collect_error = collect_error
        self.destroy_error = destroy_error

    async def provision(self, task):
        self.events.append("provision")
        return {"lease_id": task["task_id"]}

    async def execute(self, lease, task):
        self.events.append("execute")
        if self.execute_error:
            raise self.execute_error
        return {"status": "completed", "raw": "vm-result"}

    async def collect(self, lease, result):
        self.events.append("collect")
        if self.collect_error:
            raise self.collect_error
        return {
            "status": "completed",
            "worker_id": lease["lease_id"],
            "artifact_digests": [],
            "artifacts": [],
        }

    async def destroy(self, lease):
        self.events.append("destroy")
        if self.destroy_error:
            raise self.destroy_error


@pytest.mark.asyncio
async def test_disposable_vm_lifecycle_destroys_after_success():
    backend = FakeBackend()
    runner = DisposableVMRunner(backend)
    result = await runner({"task_id": "job-1"})
    assert result["worker_id"] == "job-1"
    assert backend.events == ["provision", "execute", "collect", "destroy"]


@pytest.mark.asyncio
async def test_disposable_vm_lifecycle_destroys_after_execution_failure():
    backend = FakeBackend(execute_error=RuntimeError("worker failed"))
    runner = DisposableVMRunner(backend)
    with pytest.raises(RuntimeError, match="worker failed"):
        await runner({"task_id": "job-2"})
    assert backend.events == ["provision", "execute", "destroy"]


@pytest.mark.asyncio
async def test_disposable_vm_lifecycle_destroys_after_execution_timeout():
    backend = FakeBackend()
    async def slow_execute(lease, task):
        backend.events.append("execute")
        await asyncio.sleep(0.1)
        return {"status": "completed"}
    backend.execute = slow_execute
    runner = DisposableVMRunner(backend, execution_timeout=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await runner({"task_id": "job-3"})
    assert backend.events == ["provision", "execute", "destroy"]


@pytest.mark.asyncio
async def test_teardown_failure_prevents_success_result():
    backend = FakeBackend(destroy_error=RuntimeError("destroy failed"))
    runner = DisposableVMRunner(backend)
    with pytest.raises(VMRunnerError, match="teardown failed"):
        await runner({"task_id": "job-4"})
    assert backend.events[-1] == "destroy"


@pytest.mark.asyncio
async def test_collection_failure_still_destroys_vm():
    backend = FakeBackend(collect_error=RuntimeError("collect failed"))
    runner = DisposableVMRunner(backend)
    with pytest.raises(RuntimeError, match="collect failed"):
        await runner({"task_id": "job-5"})
    assert backend.events == ["provision", "execute", "collect", "destroy"]


def test_nonpositive_timeouts_are_rejected():
    with pytest.raises(ValueError):
        DisposableVMRunner(FakeBackend(), execution_timeout=0)
