"""Boundary validation and unknown-job behavior for the durable queue."""
import asyncio

import pytest

from avrone.jobqueue import JobQueue


def test_enqueue_rejects_invalid_kind_payload_and_attempts(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.sqlite")
        with pytest.raises(ValueError, match="kind"):
            await q.enqueue(" ", {})
        with pytest.raises(ValueError, match="payload"):
            await q.enqueue("run", ["not", "a", "dict"])
        for attempts in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match="positive integer"):
                await q.enqueue("run", {}, max_attempts=attempts)
        await q.close()
    asyncio.run(go())


def test_fail_unknown_job_is_reported_as_missing(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.sqlite")
        assert await q.fail("does-not-exist", "worker error") == "missing"
        assert await q.get("does-not-exist") is None
        await q.close()
    asyncio.run(go())


def test_single_attempt_job_becomes_dead_on_first_failure(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "jobs.sqlite")
        jid = await q.enqueue("run", {"task": "x"}, max_attempts=1)
        claimed = await q.claim()
        assert claimed and claimed["id"] == jid and claimed["attempts"] == 1
        assert await q.fail(jid, "expected failure") == "dead"
        row = await q.get(jid)
        assert row["status"] == "dead" and row["error"] == "expected failure"
        await q.close()
    asyncio.run(go())
