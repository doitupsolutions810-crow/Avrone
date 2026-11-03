"""Durable job queue (SQLite, WAL): work survives a restart and retries on failure.

A single-file queue so AVRONE needs no external broker to start. Jobs are claimed
atomically, run, then marked done or retried with backoff; a crash leaves a job
'running' and claim() recovers it after a lease timeout.
For a large fleet, swap this for NATS JetStream or Redis Streams behind the same API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("avrone.jobqueue")

RESULT_MAX = 100000
ERROR_MAX = 4000
PAYLOAD_MAX = 4 * 1024 * 1024


def _encode_result(result: Any) -> str:
    """Always valid JSON. The old `json.dumps(result)[:100000]` raised TypeError on a non-JSON
    result (the finished job was then failed and RE-RUN) and cut long results mid-token, so get()
    crashed on the stored text forever after."""
    try:
        text = json.dumps(result, default=str, allow_nan=False, ensure_ascii=False)
        size = len(text.encode("utf-8"))
    except Exception:  # user objects can raise from __str__ or __repr__ too
        try:
            fallback = repr(result)
        except Exception:
            fallback = "<unrepresentable result>"
        text = json.dumps(fallback, ensure_ascii=True)
        size = len(text)
    if size > RESULT_MAX:
        # Budget the encoded wrapper, including a second layer of escaping.
        head = text[:RESULT_MAX - 200]
        while True:
            text = json.dumps({"truncated": True, "bytes": size, "head": head}, ensure_ascii=False)
            if len(text.encode("utf-8")) <= RESULT_MAX:
                break
            head = head[:len(head) // 2]
    return text


def _decode(text: Any) -> Any:
    if not text:
        return None
    try:
        value = json.loads(text, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_keys)
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"corrupt": True, "raw": str(text)[:2000]}


def _err_text(error: Any) -> str:
    try:
        text = error if isinstance(error, str) else str(error)
    except Exception:
        text = "<unrepresentable error>"
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")[:ERROR_MAX]


def _reject_constant(value: str):
    raise ValueError("non-finite JSON constant")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _payload_json(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    text = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    if len(text.encode("utf-8")) > PAYLOAD_MAX:
        raise ValueError("job payload exceeds size limit")
    return text


class LeaseLost(RuntimeError):
    """A running handler must stop because it no longer owns its queue attempt."""

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
        try:
            lease = float(lease_s)
        except (TypeError, ValueError, OverflowError):
            lease = 900.0
        # a zero, negative or NaN lease made every running job instantly reclaimable (double runs)
        self.lease_s = lease if math.isfinite(lease) and lease >= 1 else 900.0
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # Several JobQueue instances (processes) may share one file: wait for the write lock
        # instead of failing with "database is locked".
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "token" not in cols:  # migrate a pre-round-29 file in place
            try:
                self._db.execute("ALTER TABLE jobs ADD COLUMN token TEXT")
            except sqlite3.OperationalError:
                # Only ignore a concurrent successful migration, not an I/O error.
                if "token" not in {r[1] for r in self._db.execute("PRAGMA table_info(jobs)")}:
                    raise
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
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
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
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        body = _payload_json(payload)  # reject bad work before it becomes durable
        kind = str(kind)
        if not kind.strip() or len(kind) > 128 or "\x00" in kind:
            raise ValueError("job kind is invalid")
        kind.encode("utf-8")
        try:
            prio = max(-(2 ** 63), min(int(priority), 2 ** 63 - 1))
        except (TypeError, ValueError, OverflowError):
            prio = 0
        try:
            tries = max(1, min(int(max_attempts), 1000))
        except (TypeError, ValueError, OverflowError):
            tries = 3
        await self._run(lambda: self._db.execute(
            "INSERT INTO jobs(id,kind,payload,status,priority,max_attempts,created,updated) VALUES(?,?,?,?,?,?,?,?)",
            (jid, kind, body, "queued", prio, tries, now, now)))
        return jid

    async def claim(self) -> dict[str, Any] | None:
        def _claim() -> Any:
            now = time.time()
            # A job whose lease ran out after its LAST attempt crashed its worker every time
            # (a poison job): it used to be re-claimed forever. It goes dead instead.
            self._db.execute(
                "UPDATE jobs SET status='dead', updated=?, error=COALESCE(error || ' | ', '') || "
                "'lease expired on the last attempt (worker crashed or hung)', token=NULL "
                "WHERE status='running' AND lease_until<=? AND attempts>=max_attempts", (now, now))
            while True:
                cur = self._db.execute(
                    "SELECT id,kind,payload,attempts FROM jobs WHERE (status='queued' AND lease_until<=?) "
                    "OR (status='running' AND lease_until<=?) ORDER BY priority DESC, created, id LIMIT 1", (now, now))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    if not isinstance(row[2], str) or len(row[2].encode("utf-8")) > PAYLOAD_MAX:
                        raise ValueError("invalid payload size or storage type")
                    payload = json.loads(row[2], parse_constant=_reject_constant,
                                         object_pairs_hook=_unique_keys)
                    _payload_json(payload)
                except (TypeError, ValueError, UnicodeError, RecursionError) as e:
                    # used to raise out of claim() after COMMIT and kill the Worker loop for good
                    self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?",
                                     (_err_text(f"corrupt payload: {e}"), now, row[0]))
                    continue
                token = uuid.uuid4().hex
                self._db.execute("UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated=?, "
                                 "token=? WHERE id=?", (now + self.lease_s, now, token, row[0]))
                return row, payload, token
        got = await self._run(lambda: self._txn(_claim))
        if got is None:
            return None
        row, payload, token = got
        return {"id": row[0], "kind": row[1], "payload": payload, "attempts": row[3] + 1, "token": token}

    @staticmethod
    def _owner(token: str | None) -> tuple[str, tuple]:
        """Fence for writes from a claimer: a worker whose lease ran out (and whose job another
        worker re-claimed) must not mark that job done, requeue it, or overwrite its result."""
        if token is None:
            # Legacy local callers omit the token, but cannot rewrite terminal jobs.
            return " AND status='running'", ()
        return " AND status='running' AND token=? AND lease_until>?", (token, time.time())

    async def complete(self, jid: str, result: Any, token: str | None = None) -> bool:
        """True when the result was recorded; False when the lease was lost (someone else owns it)."""
        text = _encode_result(result)
        def _complete() -> bool:
            clause, extra = self._owner(token)
            cur = self._db.execute(
                f"UPDATE jobs SET status='done', result=?, updated=?, lease_until=0 WHERE id=?{clause}",
                (text, time.time(), jid, *extra))
            return cur.rowcount > 0
        return await self._run(lambda: self._txn(_complete))

    async def fail(self, jid: str, error: Any, token: str | None = None) -> str:
        """'dead', 'requeued', 'missing' (no such job) or 'stale' (the lease was lost)."""
        err = _err_text(error)
        def _fail() -> str:
            now = time.time()
            clause, extra = self._owner(token)
            r = self._db.execute(f"SELECT attempts,max_attempts FROM jobs WHERE id=?{clause}", (jid, *extra)).fetchone()
            if r is None:
                exists = self._db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
                return "stale" if exists else "missing"
            if r[0] >= r[1]:
                self._db.execute("UPDATE jobs SET status='dead', error=?, updated=? WHERE id=?", (err, now, jid))
                return "dead"
            backoff = min(2 ** min(6, max(1, int(r[0]))), 60)
            self._db.execute("UPDATE jobs SET status='queued', error=?, lease_until=?, updated=? WHERE id=?",
                             (err, now + backoff, now, jid))
            return "requeued"
        return await self._run(lambda: self._txn(_fail))

    async def extend(self, jid: str, token: str, seconds: float | None = None) -> bool:
        """Heartbeat: push a running job's lease out. False when the lease is already lost."""
        try:
            duration = self.lease_s if seconds is None else float(seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("heartbeat duration must be finite and positive") from None
        if isinstance(seconds, bool) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("heartbeat duration must be finite and positive")
        if not math.isfinite(time.time() + duration):
            raise ValueError("heartbeat duration exceeds the clock range")
        def _extend() -> bool:
            now = time.time()
            cur = self._db.execute(
                "UPDATE jobs SET lease_until=MAX(lease_until,?), updated=? "
                "WHERE id=? AND status='running' AND token=? AND lease_until>?",
                (now + duration, now, jid, token, now))
            return cur.rowcount > 0
        return await self._run(lambda: self._txn(_extend))

    async def release(self, jid: str, token: str) -> bool:
        """Give a claimed job back untouched (worker shutting down): queued again right away and
        the attempt is not counted. Before, a cancelled job sat 'running' for the whole lease."""
        def _release() -> bool:
            now = time.time()
            cur = self._db.execute(
                "UPDATE jobs SET status='queued', attempts=MAX(attempts-1,0), lease_until=0, token=NULL, updated=? "
                "WHERE id=? AND status='running' AND token=? AND lease_until>?", (now, jid, token, now))
            return cur.rowcount > 0
        return await self._run(lambda: self._txn(_release))

    async def get(self, jid: str) -> dict[str, Any] | None:
        def _g() -> dict[str, Any] | None:
            cols = ["id", "kind", "payload", "status", "attempts", "max_attempts", "created", "updated", "result", "error"]
            r = self._db.execute(f"SELECT {','.join(cols)} FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return None
            d = dict(zip(cols, r))
            for k in ("payload", "result"):
                d[k] = _decode(d[k])  # one corrupt row no longer makes get() raise forever
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
        self.q, self.handler = queue, handler
        try:
            idle = float(idle_s)
        except (TypeError, ValueError, OverflowError):
            idle = 0.5
        self.idle_s = max(0.001, idle) if math.isfinite(idle) and idle >= 0 else 0.5
        self._task: asyncio.Task | None = None

    async def _heartbeat(self, jid: str, token: str) -> None:
        # extend at a third of the lease so a long job is never re-claimed (and run twice) mid-flight
        every = max(0.05, self.q.lease_s / 3)
        while True:
            await asyncio.sleep(every)
            try:
                if not await self.q.extend(jid, token):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("job %s: lease heartbeat failed", jid, exc_info=True)
                return  # ownership is unknown; stop work rather than run without a lease

    async def _claim(self) -> dict[str, Any] | None:
        # Cancellation of to_thread does not stop SQLite. Drain the atomic claim
        # before shutdown, then release its token if it committed in the meantime.
        claim = asyncio.create_task(self.q.claim())
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError:
            try:
                job = await claim
                if job and job.get("token"):
                    await asyncio.shield(self.q.release(job["id"], job["token"]))
            except Exception:
                log.warning("could not recover cancelled claim", exc_info=True)
            raise

    async def _execute(self, job: dict[str, Any]) -> Any:
        work = asyncio.ensure_future(self.handler(job["kind"], job["payload"]))
        beat = asyncio.create_task(self._heartbeat(job["id"], job["token"]))
        try:
            done, _ = await asyncio.wait((work, beat), return_when=asyncio.FIRST_COMPLETED)
            if work not in done:
                raise LeaseLost("job lease was lost during execution")
            return await work
        finally:
            for task in (work, beat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, beat, return_exceptions=True)

    async def _loop(self) -> None:
        errors = 0
        while True:
            try:
                job = await self._claim()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # "database is locked", a full disk, an I/O error: these used to end the loop task
                # silently, and the node stopped running queued work until restart
                if getattr(self.q, "_closed", False):
                    return
                errors += 1
                log.warning("job queue claim failed (%d in a row)", errors, exc_info=True)
                await asyncio.sleep(min(self.idle_s * (2 ** min(errors, 6)), 30))
                continue
            errors = 0
            if job is None:
                await asyncio.sleep(self.idle_s)
                continue
            jid, token = job["id"], job.get("token")
            try:
                result = await self._execute(job)
                await self.q.complete(jid, result, token)
            except asyncio.CancelledError:
                if token:
                    try:  # hand the job back so the next worker can run it now, not after the lease
                        await asyncio.shield(self.q.release(jid, token))
                    except BaseException:  # noqa: BLE001
                        pass
                raise
            except LeaseLost:
                log.warning("job %s: handler stopped after lease loss", jid)
            except Exception as e:  # noqa: BLE001
                try:
                    await self.q.fail(jid, f"{type(e).__name__}: {e}", token)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.warning("job %s: could not record failure", jid, exc_info=True)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
