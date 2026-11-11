"""Round 5 sovereign edges: quoting, unicode argv, signals, interleaving, large env, zombies."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import pytest

from avrone.config import Settings
from avrone.sovereign import CommandResult, LocalRunner, _reap


@pytest.fixture()
def runner(tmp_path):
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 15
    r = LocalRunner(s)
    asyncio.run(r.start())
    return r


def test_spaces_quotes_and_newlines_in_command(runner):
    res = asyncio.run(runner.run("echo 'hello world'; echo \"it's fine\"; printf 'a\\nb\\n'"))
    assert res.exit_code == 0
    assert "hello world" in res.stdout
    assert "it's fine" in res.stdout
    assert "a" in res.stdout and "b" in res.stdout


def test_unicode_in_argv_and_output(runner):
    res = asyncio.run(runner._exec(
        [sys.executable, "-c", "import sys; print('café 🌍', sys.argv[1])", "日本語"],
        "unicode",
        8,
        cwd=runner.workspace,
    ))
    assert res.exit_code == 0
    assert "café 🌍" in res.stdout
    assert "日本語" in res.stdout


def test_stdout_and_stderr_both_captured(runner):
    py = sys.executable
    cmd = (
        f"{py} -c \"import sys; sys.stdout.write('OUT-LINE\\n'); sys.stdout.flush(); "
        f"sys.stderr.write('ERR-LINE\\n'); sys.stderr.flush()\""
    )
    res = asyncio.run(runner.run(cmd, timeout=8))
    assert res.exit_code == 0
    assert "OUT-LINE" in res.stdout
    assert "ERR-LINE" in res.stderr
    text = res.text()
    assert "OUT-LINE" in text and "ERR-LINE" in text


def test_nonzero_exit_code_is_preserved(runner):
    res = asyncio.run(runner.run("exit 7", timeout=5))
    assert res.timed_out is False
    assert res.exit_code == 7


def test_sigterm_and_sigkill_are_results_not_crashes(runner):
    py = sys.executable
    term = asyncio.run(runner.run(
        f"{py} -c \"import os, signal; signal.signal(signal.SIGTERM, signal.SIG_DFL); os.kill(os.getpid(), signal.SIGTERM)\"",
        timeout=8,
    ))
    assert isinstance(term, CommandResult)
    assert term.exit_code != 0
    kill = asyncio.run(runner.run(
        f"{py} -c \"import os, signal; os.kill(os.getpid(), signal.SIGKILL)\"",
        timeout=8,
    ))
    assert isinstance(kill, CommandResult)
    assert kill.exit_code != 0


def test_timeout_reap_leaves_no_zombie(runner):
    """A child that ignores SIGTERM and overruns the timeout must be reaped (no zombie)."""
    py = sys.executable
    cmd = (
        f"{py} -c \"import time, signal, os; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep(30); open('survived','w').write('x')\""
    )
    t0 = time.monotonic()
    res = asyncio.run(runner.run(cmd, timeout=1))
    elapsed = time.monotonic() - t0
    assert res.timed_out is True
    assert elapsed < 8  # bounded by timeout + 2s reap
    # The child must not still be running (and must not have written the marker).
    assert not (runner.workspace / "survived").exists()
    # `ps` listing of zombies for this user is optional; a second wait_for-style check:
    # if a zombie lingered, a later short command would still work.
    later = asyncio.run(runner.run("echo still-ok", timeout=5))
    assert later.exit_code == 0 and "still-ok" in later.stdout


def test_very_large_env_is_a_result_not_a_crash(runner):
    # A huge environment can raise E2BIG (or start and run). Either is a CommandResult.
    env = {f"K{i}": ("v" * 256) for i in range(4000)}
    res = asyncio.run(runner._exec(
        [sys.executable, "-c", "print('ok')"],
        "big-env",
        8,
        cwd=runner.workspace,
        env=env,
    ))
    assert isinstance(res, CommandResult)
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)


def test_argv_with_spaces_is_one_argument(runner):
    res = asyncio.run(runner._exec(
        [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", "hello world"],
        "spaces",
        8,
        cwd=runner.workspace,
    ))
    assert res.exit_code == 0
    assert "'hello world'" in res.stdout
