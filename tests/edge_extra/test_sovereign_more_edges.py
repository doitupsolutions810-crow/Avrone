"""Round 3 sovereign edges: vanished cwd, NUL env, timeout-bounded reap of a stalled wait()."""
from __future__ import annotations

import asyncio
import os
import shutil
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


def test_workspace_removed_before_run_is_a_result(runner, tmp_path):
    """If the workspace disappears after start(), run() must return a CommandResult, not raise."""
    shutil.rmtree(tmp_path)
    res = asyncio.run(runner.run("echo hi", timeout=5))
    assert isinstance(res, CommandResult)
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)


def test_child_cwd_vanishes_mid_run_is_a_result(runner):
    """A child that rmdirs its own cwd must finish as a CommandResult (stdout/stderr still str)."""
    res = asyncio.run(runner.run(
        'd=$(mktemp -d) && cd "$d" && rmdir "$d" && echo survived',
        timeout=8,
    ))
    assert isinstance(res, CommandResult)
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert "survived" in res.stdout or res.exit_code != 0


def test_env_with_nul_is_a_result_not_a_crash(runner):
    """create_subprocess_exec rejects env values with NUL; _exec must surface that as a result."""
    res = asyncio.run(runner._exec(
        ["bash", "-lc", "echo hi"],
        "echo hi",
        5,
        cwd=runner.workspace,
        env={"CORRUPT": "pre\x00post"},
    ))
    assert isinstance(res, CommandResult)
    assert res.exit_code != 0
    assert "could not start" in res.stderr or "NUL" in res.stderr or "null" in res.stderr.lower()


def test_missing_path_env_still_runs_or_returns_result(runner, monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    res = asyncio.run(runner.run("echo still-here", timeout=8))
    assert isinstance(res, CommandResult)
    assert isinstance(res.stdout, str)


def test_reap_bounds_a_hung_wait():
    """A child whose wait() never completes must not pin the loop past the 2s reap cap."""

    class Hung:
        returncode = None

        def kill(self):
            return None

        async def wait(self):
            await asyncio.sleep(30)

    async def go():
        t0 = time.monotonic()
        await _reap(Hung())
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    assert elapsed < 5.0, f"_reap hung for {elapsed:.2f}s"
    assert elapsed >= 1.5, f"_reap returned too fast ({elapsed:.2f}s); wait_for timeout not exercised"
