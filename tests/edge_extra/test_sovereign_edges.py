"""LocalRunner / _exec edges: SIGPIPE, missing HOME, read-only cwd, non-UTF-8 output."""
from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from avrone.config import Settings
from avrone.sovereign import LocalRunner


@pytest.fixture()
def runner(tmp_path):
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 15
    r = LocalRunner(s)
    asyncio.run(r.start())
    return r


def test_sigpipe_child_is_a_result(runner):
    """A child that dies of SIGPIPE must not raise out of run(); it is a CommandResult."""
    py = sys.executable
    cmd = (
        f"{py} -c \"import os, signal; signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
        f"os.kill(os.getpid(), signal.SIGPIPE)\""
    )
    res = asyncio.run(runner.run(cmd, timeout=10))
    assert res.timed_out is False
    # POSIX: SIGPIPE is 13, shells often report 128+13=141 or a negative returncode.
    assert res.exit_code != 0
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)


def test_missing_home_still_runs(runner, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    res = asyncio.run(runner.run("echo ok-no-home", timeout=10))
    assert res.exit_code == 0
    assert "ok-no-home" in res.stdout


def test_readonly_cwd_echo_still_works(runner, tmp_path):
    os.chmod(tmp_path, 0o555)
    try:
        res = asyncio.run(runner.run("echo still-ok", timeout=10))
        assert res.exit_code == 0
        assert "still-ok" in res.stdout
        # writing in a read-only workspace must fail as a result, not a crash
        res = asyncio.run(runner.run("echo x > cannot_write.txt", timeout=10))
        assert res.exit_code != 0
        assert not (tmp_path / "cannot_write.txt").exists()
    finally:
        os.chmod(tmp_path, 0o755)


def test_non_utf8_stdout_is_replaced(runner):
    # printf a lone 0xff byte; decode(errors="replace") must keep run() from raising
    res = asyncio.run(runner.run("printf '\\xff'", timeout=10))
    assert res.exit_code == 0
    assert "\ufffd" in res.stdout or res.stdout == "\xff"


def test_stdin_is_devnull_so_child_cannot_block_on_input(runner):
    """A command that reads stdin must see EOF immediately (stdin=DEVNULL), not hang."""
    res = asyncio.run(runner.run("cat; echo after-eof", timeout=8))
    assert res.timed_out is False
    assert res.exit_code == 0
    assert "after-eof" in res.stdout


def test_closed_stdout_then_write_is_a_result(runner):
    """Closing stdout and writing again typically SIGPIPEs or errors; must be a result."""
    res = asyncio.run(runner.run("exec >&-; echo still-trying", timeout=10))
    assert res.timed_out is False
    assert isinstance(res.stdout, str)
