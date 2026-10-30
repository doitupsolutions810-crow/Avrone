"""Contract tests for the validation -> policy -> isolated-runner -> verifier path."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from avrone.task_contract import EnvelopeError
from avrone.vertical_slice import execute_vertical_slice


NOW = datetime(2026, 10, 30, 4, 5, tzinfo=timezone.utc)


def envelope():
    return {
        "schema_version": 1,
        "correlation_id": "corr-e2e-1",
        "task_id": "task-e2e-1",
        "source": {"repository": "owner/repo", "commit_sha": "a" * 40},
        "requested_action": "run_tests",
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "requested_capabilities": ["read_repository"],
        "resource_limits": {"timeout_seconds": 30, "memory_mb": 256, "output_bytes": 4096},
        "input_artifact_digests": [],
        "deadline": (NOW + timedelta(seconds=30)).isoformat(),
    }


@pytest.mark.asyncio
async def test_happy_path_requires_policy_allow_and_independent_verifier():
    calls = []

    async def authorize(task):
        calls.append(("authorize", task["task_id"]))
        return {"decision": "ALLOW", "policy_version": "test-policy-v1"}

    async def runner(task):
        calls.append(("runner", task["task_id"]))
        return {"status": "completed", "worker_id": "disposable-test-worker", "artifact_digests": ["b" * 64]}

    async def verifier(task, result):
        calls.append(("verify", task["task_id"]))
        return result["artifact_digests"] == ["b" * 64]

    result = await execute_vertical_slice(
        envelope(), allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, now=NOW,
    )
    assert result["state"] == "VERIFIED"
    assert [item[0] for item in calls] == ["authorize", "runner", "verify"]
    assert result["receipt"]["source"]["commit_sha"] == "a" * 40
    assert result["receipt"]["authorization"] == "ALLOW"


@pytest.mark.asyncio
async def test_denial_never_calls_runner_or_verifier():
    calls = []

    async def authorize(task):
        calls.append("authorize")
        return {"decision": "DENY", "policy_version": "deny-policy"}

    async def runner(task):
        calls.append("runner")
        raise AssertionError("denied task must not run")

    async def verifier(task, result):
        calls.append("verify")
        return True

    result = await execute_vertical_slice(
        envelope(), allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, now=NOW,
    )
    assert result["state"] == "DENIED"
    assert calls == ["authorize"]


@pytest.mark.asyncio
async def test_authorization_outage_fails_closed_without_dispatch():
    async def authorize(task):
        raise ConnectionError("policy service unavailable")

    async def runner(task):
        pytest.fail("runner must not be called without authorization")

    async def verifier(task, result):
        pytest.fail("verifier must not be called without execution")

    result = await execute_vertical_slice(
        envelope(), allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, now=NOW,
    )
    assert result["state"] == "UNKNOWN"
    assert result["reason"] == "authorization_unavailable"


@pytest.mark.asyncio
async def test_invalid_envelope_fails_before_authorization():
    async def never(task):
        pytest.fail("no downstream service should be called")

    task = envelope()
    task["requested_capabilities"] = ["host_root"]
    with pytest.raises(EnvelopeError):
        await execute_vertical_slice(
            task, allowed_capabilities={"read_repository"}, authorize=never,
            run_isolated=never, verify_artifacts=never, now=NOW,
        )


@pytest.mark.asyncio
async def test_verification_failure_never_reports_verified():
    async def authorize(task):
        return {"decision": "ALLOW", "policy_version": "policy-v1"}

    async def runner(task):
        return {"status": "completed", "worker_id": "worker-1", "artifact_digests": ["b" * 64]}

    async def verifier(task, result):
        return False

    result = await execute_vertical_slice(
        envelope(), allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, now=NOW,
    )
    assert result["state"] == "VERIFICATION_FAILED"
    assert "receipt" not in result


@pytest.mark.asyncio
async def test_runner_completes_then_requires_verification():
    async def authorize(task):
        return {"decision": "ALLOW", "policy_version": "policy-v1"}

    async def runner(task):
        await asyncio.sleep(0.05)
        return {"status": "completed", "worker_id": "worker-1", "artifact_digests": []}

    async def verifier(task, result):
        return True

    result = await execute_vertical_slice(
        envelope(), allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, policy_timeout_seconds=1,
        now=NOW,
    )
    assert result["state"] == "VERIFIED"


@pytest.mark.asyncio
async def test_runner_timeout_is_not_success():
    async def authorize(task):
        return {"decision": "ALLOW", "policy_version": "policy-v1"}

    async def runner(task):
        await asyncio.sleep(1.1)
        return {"status": "completed", "worker_id": "worker-1", "artifact_digests": []}

    async def verifier(task, result):
        return True

    task = envelope()
    task["resource_limits"]["timeout_seconds"] = 1
    result = await execute_vertical_slice(
        task, allowed_capabilities={"read_repository"}, authorize=authorize,
        run_isolated=runner, verify_artifacts=verifier, now=NOW,
    )
    assert result["state"] == "TIMED_OUT"
    assert "receipt" not in result
