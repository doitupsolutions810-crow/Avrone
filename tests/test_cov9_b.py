"""Round 9 coverage: recon gate edges, doctor helpers, jobqueue rollback/cancel, container reap errors."""
import asyncio
from pathlib import Path

import pytest

from avrone import agentcontainer as ac
from avrone import doctor, recon
from avrone.jobqueue import JobQueue, Worker


# ------------------------------------------------------------------ recon gate
def test_reject_private_empty_and_bad_embedded_quad():
    assert recon._reject_private("") == "ERROR: empty target"
    assert recon._reject_private("999.1.1.1.example.com") is None  # not a real IPv4: name stays allowed
    assert "embeds a private" in recon._reject_private("10.0.0.1.nip.io")


def test_reject_resolved_unparseable():
    assert "unparseable" in recon._reject_resolved("x.com", ["not-an-ip"])
    assert "did not resolve" in recon._reject_resolved("x.com", [])
    assert recon._reject_resolved("x.com", ["93.184.216.34"]) is None


def test_resolve_ips_error_and_dedupe(monkeypatch):
    monkeypatch.setattr(recon.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert recon._resolve_ips("x.com") == []
    infos = [(0, 0, 0, "", ("1.1.1.1", 0)), (0, 0, 0, "", ("1.1.1.1", 0)), (0, 0, 0, "", ("1.0.0.1", 0))]
    monkeypatch.setattr(recon.socket, "getaddrinfo", lambda *a, **k: infos)
    assert recon._resolve_ips("x.com") == ["1.0.0.1", "1.1.1.1"]


def test_host_arg_empty_authority_and_idna_recheck(monkeypatch):
    assert recon._host_arg({"host": "https:///path"})[1] == 'ERROR: give {"host": "example.com"}'
    calls = []
    real = recon._reject_private

    def spy(h):
        calls.append(h)
        return "ERROR: late refusal" if len(calls) == 2 else real(h)
    monkeypatch.setattr(recon, "_reject_private", spy)
    assert recon._host_arg({"host": "example.com"})[1] == "ERROR: late refusal"


# ------------------------------------------------------------------ doctor
def test_cleartext_remote_bad_url():
    assert doctor._cleartext_remote("http://[::1") is False
    assert doctor._cleartext_remote("http://example.com") is True
    assert doctor._cleartext_remote("http://host.docker.internal:11434") is False


def test_flag_and_nearest_existing(tmp_path):
    assert doctor._flag({}, "X", True) is True
    assert doctor._flag({"X": "yes"}, "X", False) is True
    assert doctor._nearest_existing(tmp_path / "a" / "b") == tmp_path


def test_nearest_existing_root_missing(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert doctor._nearest_existing(Path("/nope/x")) is None


# ------------------------------------------------------------------ jobqueue
def test_txn_rolls_back_on_error(tmp_path):
    q = JobQueue(tmp_path / "q.db")

    async def go():
        jid = await q.enqueue("k", {"a": 1})

        def bad():
            q._db.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
            raise RuntimeError("mid-txn")
        with pytest.raises(RuntimeError):
            await q._run(lambda: q._txn(bad))
        st = (await q.get(jid))["status"]
        await q.close()
        return st
    assert asyncio.run(go()) == "queued"


def test_worker_propagates_cancel(tmp_path):
    q = JobQueue(tmp_path / "q.db")

    async def go():
        ev = asyncio.Event()

        async def handler(kind, payload):
            ev.set()
            await asyncio.sleep(10)
        w = Worker(q, handler, idle_s=0.01)
        await q.enqueue("k", {})
        t = asyncio.create_task(w._loop())
        await asyncio.wait_for(ev.wait(), 5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        stats = await q.stats()
        await q.close()
        return stats
    stats = asyncio.run(go())
    assert stats.get("failed", 0) == 0  # a cancel is not recorded as a job failure


# ------------------------------------------------------------------ agentcontainer
def test_reap_and_close_survive_rm_errors(tmp_path):
    async def exec_fn(argv, timeout):
        raise RuntimeError("docker gone")
    pool = ac.AgentContainerPool(exec_fn, "img", False, node="n", clock=lambda: 1000.0, idle=10)
    pool.homes["c1"] = ac._Home("c1", tmp_path, last_used=0.0)
    pool.homes["c2"] = ac._Home("c2", tmp_path, last_used=999.0)

    async def go():
        reaped = await pool.reap_idle()
        await pool.close()
        return reaped
    assert asyncio.run(go()) == ["c1"]
    assert pool.reaped == 0 and pool.homes == {}
