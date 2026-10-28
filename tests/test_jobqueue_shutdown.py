"""Regression: closing the queue while a cancelled claim is still inside SQLite must not crash."""
import asyncio
import sqlite3
import threading
import time

import pytest

from avrone.jobqueue import JobQueue, Worker


def test_close_waits_for_inflight_thread(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "q.db")
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(2)
            return q._db.execute("SELECT 1").fetchone()[0]

        t = asyncio.create_task(q._run(slow))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        t.cancel()  # releases the asyncio lock while the thread is still in SQLite
        closer = asyncio.create_task(q.close())
        await asyncio.sleep(0.05)
        assert not closer.done()  # close must wait for the in-flight thread
        release.set()
        await closer
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())


def test_calls_after_close_fail_cleanly(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "q.db")
        await q.close()
        await q.close()  # idempotent
        with pytest.raises(sqlite3.ProgrammingError):
            await q.stats()
    asyncio.run(go())


def test_worker_stop_then_close_repeatedly(tmp_path):
    async def go():
        for i in range(25):
            q = JobQueue(tmp_path / f"q{i}.db")
            await q.enqueue("k", {"i": i})
            w = Worker(q, lambda k, p: asyncio.sleep(0, result=p), idle_s=0)
            w.start()
            await asyncio.sleep(0)
            await w.stop()
            await q.close()
    asyncio.run(go())
