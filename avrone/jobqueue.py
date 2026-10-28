"""Durable job queue (SQLite, WAL): work survives a restart and retries on failure.

A single-file queue so AVRONE needs no external broker to start. Jobs are claimed
atomically, run, then marked done or retried with backoff; a crash leaves a job
'running' and reap() returns it to the queue after a lease timeout.
For a large fleet, swap this for NATS JetStream or Redis Streams behind the same API.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
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


class JobQueue:
    def __init__(self, path: Path, lease_s: float = 900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_s = lease_s
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = asyncio.Lock()

    async def _run(self, fn: Callable[[], Any]) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn)

    async def enqueue(self, kind: str, payload: dict[str, Any], priority: int = 0, max_attempts: int = 3) -> str:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        await self._run(lambda: self._db.execute(
            "INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) VALUES(?,?,?,?,?,?,?,?)",
            (jid, kind, json.dumps(payload), "queued", priority, max_attempts, now, now)))
        return jid

    async def claim(self) -> dict[str, Any] | None:
        def _claim() -> dict[str, Any] | None:
            now = time.time()
            cur = self._db.execute(
                "SELECT id,kind,payload,attempts FROM jobs WHERE (status='queued' AND lease_until<=?) OR (status='running' AND lease_until<?) "
                "ORDER BY priority DESC, created LIMIT 1", (now, now))
            row = cur.fetchone()
            if not row:
                return None
            self._db.execute("UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated=? WHERE id=?",
                             (now + self.lease_s, now, row[0]))
            return {"id": row[0], "kind": row[1], "payload": json.loads(row[2]), "attempts": row[3] + 1}
        return await self._run(_claim)

    async def complete(self, jid: str, result: Any) -> None:
        await self._run(lambda: self._db.execute("UPDATE jobs SET status='done', result=?, updated=? WHERE id=?",
                                                 (json.dumps(result)[:100000], time.time(), jid)))

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
        return await self._run(_fail)

    async def get(self, jid: str) -> dict[str, Any] | None:
        def _g() -> dict[str, Any] | None:
            cols = ["id", "kind", "payload", "status", "attempts", "max_attempts", "created", "updated", "result", "error"]
            r = self._db.execute(f"SELECT {','.join(cols)} FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return None
            d = dict(zip(cols, r))
            for k in ("payload", "result"):
                d[k] = json.loads(d[k]) if d[k] else None
            return d
        return await self._run(_g)

    async def stats(self) -> dict[str, int]:
        def _s() -> dict[str, int]:
            return {k: v for k, v in self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()}
        return await self._run(_s)

    async def close(self) -> None:
        await self._run(self._db.close)


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
