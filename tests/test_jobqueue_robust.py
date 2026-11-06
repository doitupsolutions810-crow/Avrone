"""Round 29: the durable job queue under real failure classes: a stale worker writing after its
lease was re-claimed, long jobs outliving the lease, poison jobs that crash every worker, corrupt
payloads and results, unserializable or huge results, claim errors that killed the Worker loop,
cancelled jobs stuck for a whole lease, bad lease/attempt values, and an old database file."""
import asyncio
import json
import math
import sqlite3
import time

import pytest

from avrone.jobqueue import JobQueue, Worker, _encode_result


def run(c):
    return asyncio.run(c)


@pytest.fixture
def qpath(tmp_path):
    return tmp_path / "q.db"


def expire(q, jid):
    q._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))


# ------------------------------------------------------------------ lease fencing
def test_stale_worker_cannot_complete_reclaimed_job(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {})
        a = await q.claim(); expire(q, jid)
        b = await q.claim()
        assert b["id"] == jid and b["token"] != a["token"]
        assert await q.complete(jid, "from A", a["token"]) is False
        assert (await q.get(jid))["status"] == "running"
        assert await q.complete(jid, "from B", b["token"]) is True
        g = await q.get(jid); await q.close(); return g
    g = run(go())
    assert g["status"] == "done" and g["result"] == "from B"


def test_stale_worker_cannot_requeue_or_kill(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}, max_attempts=2)
        a = await q.claim(); expire(q, jid)
        b = await q.claim()
        out = await q.fail(jid, "late A", a["token"])
        st = (await q.get(jid))["status"]
        await q.close(); return out, st
    assert run(go()) == ("stale", "running")


def test_done_job_cannot_be_overwritten_by_stale_complete(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {})
        a = await q.claim(); expire(q, jid)
        b = await q.claim()
        await q.complete(jid, "B", b["token"])
        ok = await q.complete(jid, "A", a["token"])
        r = (await q.get(jid))["result"]; await q.close(); return ok, r
    assert run(go()) == (False, "B")


def test_tokenless_calls_still_work(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {})
        await q.claim()
        assert await q.complete(jid, {"x": 1}) is True
        r = await q.get(jid); await q.close(); return r
    assert run(go())["result"] == {"x": 1}


def test_fail_missing_job(qpath):
    async def go():
        q = JobQueue(qpath)
        out = await q.fail("nope", "x"); await q.close(); return out
    assert run(go()) == "missing"


def test_fail_non_string_error(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}); j = await q.claim()
        out = await q.fail(jid, ValueError("boom"), j["token"])
        e = (await q.get(jid))["error"]; await q.close(); return out, e
    out, e = run(go())
    assert out == "requeued" and "boom" in e


def test_error_is_capped(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}, max_attempts=1); j = await q.claim()
        await q.fail(jid, "e" * 10000, j["token"])
        e = (await q.get(jid))["error"]; await q.close(); return e
    assert len(run(go())) == 4000


# ------------------------------------------------------------------ heartbeat / release
def test_extend_and_release(qpath):
    async def go():
        q = JobQueue(qpath, lease_s=5)
        jid = await q.enqueue("k", {})
        j = await q.claim()
        before = q._db.execute("SELECT lease_until FROM jobs").fetchone()[0]
        assert await q.extend(jid, j["token"], 100)
        after = q._db.execute("SELECT lease_until FROM jobs").fetchone()[0]
        assert after > before + 50
        assert not await q.extend(jid, "wrong")
        assert await q.release(jid, j["token"])
        g = await q.get(jid)
        again = await q.claim()
        await q.close(); return g, again
    g, again = run(go())
    assert g["status"] == "queued" and g["attempts"] == 0
    assert again and again["attempts"] == 1


def test_long_job_is_not_reclaimed_mid_run(qpath):
    async def go():
        q = JobQueue(qpath, lease_s=1)
        runs = []

        async def slow(kind, p):
            runs.append(1)
            await asyncio.sleep(2.5)  # 2.5x the lease
            return "ok"
        jid = await q.enqueue("k", {})
        w1, w2 = Worker(q, slow, idle_s=0.05), Worker(q, slow, idle_s=0.05)
        w1.start(); await asyncio.sleep(0.1); w2.start()
        for _ in range(100):
            if (await q.get(jid))["status"] == "done":
                break
            await asyncio.sleep(0.05)
        await w1.stop(); await w2.stop()
        g = await q.get(jid); await q.close(); return g, len(runs)
    g, n = run(go())
    assert g["status"] == "done" and n == 1 and g["attempts"] == 1


def test_cancelled_job_is_released_immediately(qpath):
    async def go():
        q = JobQueue(qpath)
        started = asyncio.Event()

        async def h(kind, p):
            started.set(); await asyncio.sleep(30)
        jid = await q.enqueue("k", {})
        w = Worker(q, h, idle_s=0.01); w.start()
        await asyncio.wait_for(started.wait(), 5)
        await w.stop()
        g = await q.get(jid)
        again = await q.claim()
        await q.close(); return g, again
    g, again = run(go())
    assert g["status"] == "queued" and g["attempts"] == 0
    assert again is not None and again["id"] == g["id"]


# ------------------------------------------------------------------ poison & corrupt
def test_poison_job_goes_dead_after_last_lease(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}, max_attempts=2)
        for _ in range(2):
            j = await q.claim(); assert j; expire(q, jid)   # worker "crashes" each time
        third = await q.claim()
        g = await q.get(jid); await q.close(); return third, g
    third, g = run(go())
    assert third is None and g["status"] == "dead" and "lease expired" in g["error"]


def test_corrupt_payload_is_dead_and_next_job_runs(qpath):
    async def go():
        q = JobQueue(qpath)
        bad = await q.enqueue("k", {}, priority=5)
        good = await q.enqueue("k", {"ok": 1})
        q._db.execute("UPDATE jobs SET payload='{not json' WHERE id=?", (bad,))
        j = await q.claim()
        b = await q.get(bad); await q.close(); return j, b, good
    j, b, good = run(go())
    assert j["id"] == good and j["payload"] == {"ok": 1}
    assert b["status"] == "dead" and "corrupt payload" in b["error"]
    assert b["payload"]["corrupt"] is True


def test_worker_survives_corrupt_payload(qpath):
    async def go():
        q = JobQueue(qpath)
        bad = await q.enqueue("k", {}, priority=5)
        q._db.execute("UPDATE jobs SET payload='\\x00' WHERE id=?", (bad,))
        good = await q.enqueue("k", {"v": 2})
        w = Worker(q, lambda k, p: asyncio.sleep(0, result=p["v"] * 2), idle_s=0.01); w.start()
        for _ in range(200):
            if (await q.get(good))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        await w.stop()
        r = await q.get(good); await q.close(); return r
    assert run(go())["result"] == 4


def test_get_survives_corrupt_result(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {})
        q._db.execute("UPDATE jobs SET status='done', result='[1,2' WHERE id=?", (jid,))
        g = await q.get(jid); await q.close(); return g
    g = run(go())
    assert g["result"]["corrupt"] is True and g["result"]["raw"] == "[1,2"


# ------------------------------------------------------------------ results
class Obj:
    def __str__(self):
        return "Obj()"


@pytest.mark.parametrize("result,check", [
    ({"o": Obj()}, lambda r: r == {"o": "Obj()"}),
    ({1, 2}, lambda r: isinstance(r, str)),
    (float("nan"), lambda r: r == "nan"),
    (b"bytes", lambda r: r == "b'bytes'"),
    (None, lambda r: r is None),
    ("x" * 300000, lambda r: r["truncated"] is True and r["bytes"] > 300000),
])
def test_results_are_always_valid_json(qpath, result, check):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}); j = await q.claim()
        assert await q.complete(jid, result, j["token"])
        raw = q._db.execute("SELECT result FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        g = await q.get(jid); await q.close(); return raw, g
    raw, g = run(go())
    json.loads(raw)
    assert len(raw) <= 100000 and g["status"] == "done"
    assert check(g["result"])


def test_unserializable_result_does_not_rerun_job(qpath):
    async def go():
        q = JobQueue(qpath)
        calls = []

        async def h(k, p):
            calls.append(1); return {"handle": Obj(), "fn": print}
        jid = await q.enqueue("k", {})
        w = Worker(q, h, idle_s=0.01); w.start()
        for _ in range(200):
            if (await q.get(jid))["status"] == "done":
                break
            q._db.execute("UPDATE jobs SET lease_until=0 WHERE status='queued'")
            await asyncio.sleep(0.01)
        await w.stop()
        g = await q.get(jid); await q.close(); return g, len(calls)
    g, n = run(go())
    assert g["status"] == "done" and n == 1


def test_encode_result_large_text_keeps_head():
    out = json.loads(_encode_result("abc" * 50000))
    assert out["truncated"] and out["head"].startswith('"abcabc')


# ------------------------------------------------------------------ worker resilience
def test_worker_survives_claim_errors(qpath):
    async def go():
        q = JobQueue(qpath)
        real = q.claim
        n = {"bad": 3}

        async def flaky_claim():
            if n["bad"]:
                n["bad"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return await real()
        q.claim = flaky_claim
        jid = await q.enqueue("k", {"v": 1})
        w = Worker(q, lambda k, p: asyncio.sleep(0, result="ran"), idle_s=0.005); w.start()
        for _ in range(400):
            if (await q.get(jid))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        alive = not w._task.done()
        await w.stop()
        g = await q.get(jid); await q.close(); return g, alive
    g, alive = run(go())
    assert alive and g["result"] == "ran"


def test_worker_survives_fail_errors(qpath):
    async def go():
        q = JobQueue(qpath)

        async def broken_fail(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        q.fail = broken_fail

        async def h(k, p):
            if p.get("bad"):
                raise RuntimeError("x")
            return "ok"
        await q.enqueue("k", {"bad": 1}, priority=9)
        good = await q.enqueue("k", {})
        w = Worker(q, h, idle_s=0.005); w.start()
        for _ in range(300):
            if (await q.get(good))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        alive = not w._task.done()
        await w.stop()
        g = await q.get(good); await q.close(); return g, alive
    g, alive = run(go())
    assert alive and g["status"] == "done"


def test_worker_exits_quietly_when_queue_closed(qpath):
    async def go():
        q = JobQueue(qpath)
        w = Worker(q, lambda k, p: asyncio.sleep(0), idle_s=0.005); w.start()
        await asyncio.sleep(0.03)
        await q.close()
        await asyncio.sleep(0.05)
        done = w._task.done()
        await w.stop(); return done
    assert run(go()) is True


# ------------------------------------------------------------------ inputs & migration
@pytest.mark.parametrize("lease", [0, -5, float("nan"), float("inf"), "abc", None])
def test_bad_lease_falls_back(qpath, lease):
    q = JobQueue(qpath, lease_s=lease)
    assert q.lease_s == 900.0
    run(q.close())


@pytest.mark.parametrize("ma,exp", [(0, 1), (-3, 1), ("5", 5), ("x", 3), (10 ** 9, 1000), (None, 3)])
def test_max_attempts_clamped(qpath, ma, exp):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue("k", {}, max_attempts=ma)
        g = await q.get(jid); await q.close(); return g["max_attempts"]
    assert run(go()) == exp


def test_bad_priority_and_kind(qpath):
    async def go():
        q = JobQueue(qpath)
        jid = await q.enqueue(123, {}, priority="high")
        j = await q.claim(); await q.close(); return j
    j = run(go())
    assert j["kind"] == "123"


def test_unserializable_payload_raises_at_enqueue(qpath):
    async def go():
        q = JobQueue(qpath)
        try:
            with pytest.raises(TypeError):
                await q.enqueue("k", {"o": object()})
            return await q.stats()
        finally:
            await q.close()
    assert run(go()) == {}


def test_old_database_is_migrated(qpath):
    db = sqlite3.connect(qpath)
    db.executescript("""CREATE TABLE jobs (
      id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, priority INTEGER DEFAULT 0,
      attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
      created REAL, updated REAL, lease_until REAL DEFAULT 0, result TEXT, error TEXT);""")
    db.execute("INSERT INTO jobs VALUES('old1','k','{\"a\":1}','queued',0,0,3,1,1,0,NULL,NULL)")
    db.commit(); db.close()

    async def go():
        q = JobQueue(qpath)
        j = await q.claim()
        ok = await q.complete(j["id"], "fine", j["token"])
        await q.close()
        q2 = JobQueue(qpath)  # reopening an already-migrated file is fine too
        g = await q2.get("old1"); await q2.close(); return j, ok, g
    j, ok, g = run(go())
    assert j["payload"] == {"a": 1} and ok and g["result"] == "fine"


def test_two_queues_heartbeat_fencing_across_files(qpath):
    async def go():
        q1, q2 = JobQueue(qpath), JobQueue(qpath)
        jid = await q1.enqueue("k", {})
        a = await q1.claim(); expire(q1, jid)
        b = await q2.claim()
        assert not await q1.extend(jid, a["token"])
        assert await q2.extend(jid, b["token"])
        await q1.close(); await q2.close()
    run(go())
