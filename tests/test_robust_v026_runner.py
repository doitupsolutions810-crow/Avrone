"""v0.26 robustness: LocalRunner never crashes on a command it cannot start, and a cancelled run
kills its child process instead of orphaning it."""
import asyncio
import os
import time

import pytest

from avrone import sovereign
from avrone.config import Settings
from avrone.sovereign import LocalRunner


@pytest.fixture()
def runner(tmp_path):
    s = Settings()
    s.workspace = tmp_path
    r = LocalRunner(s)
    asyncio.run(r.start()) if asyncio.iscoroutinefunction(getattr(r, "start", None)) else None
    return r


def test_nul_byte_refused(runner):
    res = asyncio.run(runner.run("echo a\x00b"))
    assert res.exit_code == -1 and "NUL" in res.stderr and "\x00" not in res.cmd


def test_unstartable_is_a_result(runner, tmp_path):
    res = asyncio.run(runner._exec(["/no/such/binary-x"], "x", 5))
    assert res.exit_code == -1 and "could not start command: FileNotFoundError" in res.stderr
    res = asyncio.run(runner._exec(["true"], "true", 5, cwd=tmp_path / "missing"))
    assert res.exit_code == -1 and "could not start" in res.stderr


def test_cancel_kills_child(runner, tmp_path):
    pidfile = tmp_path / "pid"

    async def go():
        t = asyncio.create_task(runner.run(f"echo $$ > {pidfile}; exec sleep 30"))
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    alive = True
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert not alive, "cancelled command's child survived"


def test_reap_tolerates_exited_process():
    class P:
        returncode = None

        def kill(self):
            raise ProcessLookupError

        async def wait(self):
            raise RuntimeError("gone")
    asyncio.run(sovereign._reap(P()))  # no exception


def test_timeout_still_reports(runner):
    res = asyncio.run(runner.run("sleep 5", timeout=1))
    assert res.timed_out and res.exit_code == -1
