"""Durable job queue (SQLite, WAL): work survives a restart and retries on failure.

A single-file queue so AVRONE needs no external broker to start. Jobs are claimed
atomically, run, then marked done or retried with backoff; a crash leaves a job
'running' and reap() returns it to the queue after a lease timeout.
For a large fleet, swap this for NATS JetStream or Redis Streams behind the same API.
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, priority INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  created REAL, updated REAL, lease_until REAL DEFAULT 0, result TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority DESC, created);
"""


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity in persisted state."""
    raise ValueError(f"non-finite JSON constant: {value}")


class JobQueue:
    def __init__(self, path: Path, lease_s: float = 900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            parsed_lease = float(lease_s)
        except (TypeError, ValueError, OverflowError):
            parsed_lease = 900.0
        if isinstance(lease_s, bool) or not math.isfinite(parsed_lease) or parsed_lease <= 0:
            parsed_lease = 900.0
        # Avoid immediate reclaim loops from non-positive leases and keep the maximum
        # retry window bounded even when a misconfigured caller supplies a huge value.
        self.lease_s = max(0.1, min(7 * 86400.0, parsed_lease))
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # Several JobQueue instances (processes) may share one file: wait for the write lock
        # instead of failing with "database is locked".
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = asyncio.Lock()
        # A cancelled await releases the asyncio lock while its worker thread is still
        # inside SQLite; the thread lock keeps close() (and every other call) from
        # touching the connection until that thread is done. Without it, shutting a
        # Worker down mid-claim could segfault the process.
        self._tlock = threading.Lock()
        self._closed = False

    def _txn(self, fn: Callable[[], Any]) -> Any:
        """Run fn inside BEGIN IMMEDIATE ... COMMIT: the write lock is taken BEFORE the read, so a
        read-then-write (claim's SELECT+UPDATE, fail's attempts check+UPDATE) is atomic across every
        connection on the file, not just within this instance's asyncio lock. Without it two
        queues on one file (two worker processes) both SELECTed the same queued row and ran the
        job twice."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            out = fn()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return out

    async def _run(self, fn: Callable[[], Any]) -> Any:
        def guarded() -> Any:
            with self._tlock:
                if self._closed:
                    raise sqlite3.ProgrammingError("job queue is closed")
                return fn()
        async with self._lock:
            return await asyncio.to_thread(guarded)

    async def enqueue(self, kind: str, payload: dict[str, Any], priority: int = 0, max_attempts: int = 3) -> str:
        if not isinstance(payload, dict):
            raise TypeError("job payload must be a dict")
        # Reject non-standard NaN/Infinity values and unserialisable payloads before
        # touching SQLite, so a caller cannot enqueue a poison row accidentally.
        encoded_payload = json.dumps(payload, allow_nan=False)
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        await self._run(lambda: self._db.execute(
            "INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) VALUES(?,?,?,?,?,?,?,?)",
            (jid, kind, encoded_payload, "queued", priority, max_attempts, now, now)))
        return jid

    async def claim(self) -> dict[str, Any] | None:
        def _claim() -> Any:
            now = time.time()
            while True:
                cur = self._db.execute(
                    "SELECT id,kind,payload,attempts FROM jobs WHERE (status='queued' AND lease_until<=?) OR (status='running' AND lease_until<=?) "
                    "ORDER BY priority DESC, created LIMIT 1", (now, now))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    payload = json.loads(row[2], parse_constant=_reject_json_constant)
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not an object")
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Quarantine the poison row inside the same transaction. It no longer
                    # gets leased forever or starves valid work behind it.
                    self._db.execute(
                        "UPDATE jobs SET status='dead', attempts=attempts+1, lease_until=0, updated=?, "
                        "error='invalid persisted payload JSON or non-object payload' WHERE id=?",
                        (now, row[0]))
                    continue
                self._db.execute("UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated=? WHERE id=?",
                                 (now + self.lease_s, now, row[0]))
                return (*row, payload)

        row = await self._run(lambda: self._txn(_claim))
        if row is None:
            return None
        return {"id": row[0], "kind": row[1], "payload": row[4], "attempts": row[3] + 1}

    async def complete(self, jid: str, result: Any) -> None:
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded) > 100000:
            # Never cut the serialized JSON itself: a hard slice can make the
            # persisted result unreadable and break later get() calls.
            encoded = json.dumps({"truncated": True, "preview": encoded[:99000]}, ensure_ascii=False)
        await self._run(lambda: self._db.execute("UPDATE jobs SET status='done', result=?, updated=? WHERE id=?",
                                                 (encoded, time.time(), jid)))

    async def fail(self, jid: str, error: str) -> str:
        def _fail() -> str:
            now = time.time()
            r = self._db.execute("SELECT attempts,max_attempts FROM jobs WHERE id=?", (jid,)).fetchone()
            if r and r[0] >= r[1]:
                self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?", (error[:4000], now, jid))
                return "dead"
            backoff = min(2 ** (r[0] if r else 1), 60)
            self._db.execute("UPDATE jobs SET status='queued', error=?, lease_until=?, updated=? WHERE id=?",
                             (error[:4000], now + backoff, now, jid))
            return "requeued"
        return await self._run(lambda: self._txn(_fail))

    async def get(self, jid: str) -> dict[str, Any] | None:
        def _g() -> dict[str, Any] | None:
            cols = ["id", "kind", "payload", "status", "attempts", "max_attempts", "created", "updated", "result", "error"]
            r = self._db.execute(f"SELECT {','.join(cols)} FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return None
            d = dict(zip(cols, r))
            for k in ("payload", "result"):
                if not d[k]:
                    d[k] = None
                    continue
                try:
                    d[k] = json.loads(d[k], parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Keep status inspectable even if the database was modified or
                    # damaged outside this API. Preserve the raw bytes nowhere in output.
                    d[k] = None
                    if k == "payload":
                        d["error"] = d["error"] or "invalid persisted payload JSON"
            return d
        return await self._run(_g)

    async def stats(self) -> dict[str, int]:
        def _s() -> dict[str, int]:
            return {k: v for k, v in self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()}
        return await self._run(_s)

    async def close(self) -> None:
        def _close() -> None:
            with self._tlock:
                if not self._closed:
                    self._closed = True
                    self._db.close()
        async with self._lock:
            await asyncio.to_thread(_close)


class Worker:
    """Pulls jobs and runs them with `handler(kind, payload) -> result`."""
    def __init__(self, queue: JobQueue, handler: Callable[[str, dict], Awaitable[Any]], idle_s: float = 0.5):
        self.q, self.handler, self.idle_s = queue, handler, idle_s
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            job = await self.q.claim()
            if job is None:
                await asyncio.sleep(self.idle_s)
                continue
            try:
                await self.q.complete(job["id"], await self.handler(job["kind"], job["payload"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await self.q.fail(job["id"], f"{type(e).__name__}: {e}")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
