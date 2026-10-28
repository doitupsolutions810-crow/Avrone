import asyncio, os
os.environ.setdefault("AVRONE_LLM_MODE", "mock")
from avrone.jobqueue import JobQueue, Worker


def test_queue_retry_dead_and_durable(tmp_path):
    async def go():
        q = JobQueue(tmp_path / "j.db")
        calls = {"n": 0}

        async def flaky(kind, p):
            calls["n"] += 1
            if p.get("fail"):
                raise RuntimeError("node down")
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return {"answer": p["task"].upper()}

        ok = await q.enqueue("run", {"task": "hi"}, max_attempts=3)
        bad = await q.enqueue("run", {"task": "x", "fail": True}, max_attempts=2)
        # make retries immediate for the test
        q._db.execute("UPDATE jobs SET lease_until=0")
        w = Worker(q, flaky, idle_s=0.01)
        w.start()
        for _ in range(400):
            q._db.execute("UPDATE jobs SET lease_until=0 WHERE status='queued'")
            s = await q.stats()
            if s.get("done") == 1 and s.get("dead") == 1:
                break
            await asyncio.sleep(0.02)
        await w.stop()
        assert (await q.get(ok))["result"] == {"answer": "HI"}
        assert (await q.get(bad))["status"] == "dead"
        # crash recovery: a 'running' job whose lease expired is reclaimed after reopen
        jid = await q.enqueue("run", {"task": "y"})
        await q.claim()
        q._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (jid,))
        await q.close()
        q2 = JobQueue(tmp_path / "j.db")
        again = await q2.claim()
        assert again and again["id"] == jid and again["attempts"] == 2
        await q2.close()
    asyncio.run(go())


def test_inbox_reaches_live_agent(tmp_path, monkeypatch):
    from avrone.config import Settings
    from avrone.bootstrap import build_core, run_cleanups
    monkeypatch.setenv("AVRONE_RUNNER", "local")
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))

    async def go():
        core, cleanups = await build_core(Settings(), extras=False)
        seen = []
        orig = core._chat

        async def spy(messages, **kw):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:  # while the agent is mid-run, the operator messages it
                assert core.message_agent(next(iter(core.active)), "also build api.py", "root/operator")
            return await orig(messages, **kw)
        core._chat = spy
        await core.run_agent("r1", "Builder", "build app")
        await run_cleanups(cleanups)
        return seen
    seen = asyncio.run(go())
    assert any("MESSAGE FROM root/operator: also build api.py" in s for s in seen)
