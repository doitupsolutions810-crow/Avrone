"""Queue ownership, cancellation and storage boundaries beyond daemon round 29."""
import asyncio
import json
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from avrone.jobqueue import JobQueue, Worker, RESULT_MAX, _encode_result


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "fail", "extend", "release"])
async def test_expired_owner_cannot_write_before_reclamation(tmp_path, operation):
    q = JobQueue(tmp_path / "jobs.db")
    try:
        jid = await q.enqueue("run", {})
        job = await q.claim()
        q._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))
        if operation == "complete":
            result = await q.complete(jid, "late", job["token"])
        elif operation == "fail":
            result = await q.fail(jid, "late", job["token"])
        else:
            result = await getattr(q, operation)(jid, job["token"])
        assert result == ("stale" if operation == "fail" else False)
        assert (await q.get(jid))["status"] == "running"
    finally:
        await q.close()


@pytest.mark.parametrize("value", ["\\\"\n\x00" * 60000, "☃😀" * 60000, "\ud800", float("nan")])
def test_result_bound_applies_after_json_escaping(value):
    encoded = _encode_result(value)
    assert len(encoded.encode("utf-8")) <= RESULT_MAX
    decoded = json.loads(encoded)
    # Anything returned by get() must also be safe for a UTF-8 HTTP response.
    json.dumps(decoded, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], "text", {"n": float("nan")},
                                       {"n": float("inf")}, {"s": "\ud800"}])
async def test_invalid_payload_rejected_before_enqueue(tmp_path, payload):
    q = JobQueue(tmp_path / "jobs.db")
    try:
        with pytest.raises((TypeError, ValueError, UnicodeError)):
            await q.enqueue("run", payload)
        assert await q.stats() == {}
    finally:
        await q.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['[]', 'null', '{"n":NaN}', '{"n":1e999}',
                                    '{"n":1,"n":2}', '{"s":"\\ud800"}'])
async def test_corrupt_or_nonobject_rows_quarantined_without_starving_valid_job(tmp_path, raw):
    q = JobQueue(tmp_path / "jobs.db")
    try:
        bad = await q.enqueue("run", {}, priority=100)
        good = await q.enqueue("run", {"task": "valid"})
        q._db.execute("UPDATE jobs SET payload=? WHERE id=?", (raw, bad))
        job = await q.claim()
        assert job["id"] == good
        assert (await q.get(bad))["status"] == "dead"
    finally:
        await q.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, 0, True, "junk"])
async def test_invalid_heartbeat_duration_preserves_lease(tmp_path, seconds):
    q = JobQueue(tmp_path / "jobs.db")
    try:
        jid = await q.enqueue("run", {})
        job = await q.claim()
        before = q._db.execute("SELECT lease_until FROM jobs").fetchone()[0]
        with pytest.raises(ValueError):
            await q.extend(jid, job["token"], seconds)
        assert q._db.execute("SELECT lease_until FROM jobs").fetchone()[0] == before
    finally:
        await q.close()


@pytest.mark.asyncio
async def test_terminal_result_cannot_be_changed_by_tokenless_legacy_call(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    try:
        jid = await q.enqueue("run", {})
        job = await q.claim()
        assert await q.complete(jid, "original", job["token"])
        assert await q.complete(jid, "changed") is False
        assert await q.fail(jid, "changed") == "stale"
        assert (await q.get(jid))["result"] == "original"
    finally:
        await q.close()


@pytest.mark.asyncio
async def test_cancel_during_claim_waits_and_releases_committed_claim(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    entered, release = threading.Event(), threading.Event()
    original = q._txn

    def blocked(fn):
        entered.set()
        assert release.wait(5), "test did not release the transaction"
        return original(fn)

    q._txn = blocked
    jid = await q.enqueue("run", {})
    worker = Worker(q, AsyncMock(return_value="must not run"), idle_s=0.01)
    stopping = None
    try:
        worker.start()
        assert await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(worker.stop())
        await asyncio.sleep(0.03)
        assert not stopping.done(), "shutdown abandoned an in-flight claim"
        release.set()
        await asyncio.wait_for(stopping, 5)
        job = await q.get(jid)
        assert job["status"] == "queued" and job["attempts"] == 0
        worker.handler.assert_not_awaited()
    finally:
        release.set()
        if stopping is not None:
            await stopping
        await worker.stop()
        await q.close()


@pytest.mark.asyncio
async def test_commit_error_rolls_back_transaction(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    original = q._db

    class FailCommit:
        failed = False

        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, *args):
            if sql == "COMMIT" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("simulated commit I/O error")
            return original.execute(sql, *args)

    q._db = FailCommit()
    try:
        await q.enqueue("run", {})
        with pytest.raises(sqlite3.OperationalError, match="commit I/O"):
            await q.claim()
        assert not q._db.in_transaction
        assert (await q.claim())["attempts"] == 1
    finally:
        await q.close()


@pytest.mark.asyncio
async def test_lease_loss_cancels_running_handler_and_keeps_worker_alive(tmp_path):
    q = JobQueue(tmp_path / "jobs.db", lease_s=1)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def handler(kind, payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def lost(*args):
        return False

    q.extend = lost
    worker = Worker(q, handler, idle_s=0.01)
    try:
        await q.enqueue("run", {})
        worker.start()
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(stopped.wait(), 2)
        assert not worker._task.done()
    finally:
        await worker.stop()
        await q.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,payload", [("unknown", {"task": "x"}),
    ("run", {"task": "x", "mode": "unknown"}), ("run", {"task": " "}),
    ("run", {"task": 12}), ("message", {"agent": "A", "message": ""})])
async def test_fleet_queue_refuses_invalid_job_before_model_or_network(tmp_path, kind, payload):
    from avrone.fleet import FleetManager

    core = MagicMock()
    core.active = {}
    core.chat = AsyncMock()
    core.squad = AsyncMock()
    fm = FleetManager(core, tmp_path)
    fm.dispatch = AsyncMock()
    fm.send_message = AsyncMock()
    with pytest.raises(ValueError):
        await fm.run_job(kind, payload)
    for method in (core.chat, core.squad, fm.dispatch, fm.send_message):
        method.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_queue_deep_job_uses_deep_runtime(tmp_path):
    from avrone.fleet import FleetManager

    core = MagicMock()
    core.active = {}
    core.deep = AsyncMock(return_value={"answer": "reviewed"})
    fm = FleetManager(core, tmp_path)
    result = await fm.run_job("run", {"task": "check", "node": "local", "mode": "deep", "n": 3})
    assert result["answer"] == "reviewed"
    core.deep.assert_awaited_once_with("check", n=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "fail", "extend", "release"])
async def test_lease_checked_after_waiting_for_database_thread(tmp_path, monkeypatch, operation):
    from avrone import jobqueue

    q = JobQueue(tmp_path / "jobs.db", lease_s=1)
    original_run = q._run
    original_time = jobqueue.time.time
    try:
        jid = await q.enqueue("run", {})
        job = await q.claim()

        async def delayed(fn):
            # Simulate the clock advancing while waiting for the DB writer.
            monkeypatch.setattr(jobqueue.time, "time", lambda: original_time() + 10)
            return await original_run(fn)

        q._run = delayed
        if operation == "complete":
            result = await q.complete(jid, "late", job["token"])
        elif operation == "fail":
            result = await q.fail(jid, "late", job["token"])
        else:
            result = await getattr(q, operation)(jid, job["token"])
        assert result == ("stale" if operation == "fail" else False)
    finally:
        await q.close()
