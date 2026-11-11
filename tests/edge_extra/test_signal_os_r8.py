"""Round 8: signal/OS realism — SIGTERM ignored then SIGKILL within the reap cap,
broken pipe in both directions.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from avrone.config import Settings
from avrone.mcp_server import AvroneMCPServer
from avrone.sovereign import LocalRunner, _reap


class _FakeCore:
    def __init__(self):
        self.tools = type("T", (), {"list": lambda self: []})()

    async def chat(self, task):
        return "ok"

    async def squad(self, task, size=None):
        return {"answer": "ok", "run_id": "r", "agents": [1]}

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r"}


@pytest.mark.asyncio
async def test_ignore_sigterm_then_sigkill_within_reap_cap(tmp_path: Path) -> None:
    """A child that ignores SIGTERM must be SIGKILL'd by _reap and wait()ed,
    all inside the 2s wait cap (plus a little scheduling slack)."""
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 20
    r = LocalRunner(s)
    await r.start()
    # Ignore SIGTERM; only SIGKILL will end the process.
    py = sys.executable
    cmd = (
        f"{py} -c \"import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep(60)\""
    )
    t0 = time.monotonic()
    res = await r.run(cmd, timeout=1)
    elapsed = time.monotonic() - t0
    assert res.timed_out is True
    # timeout (1s) + SIGTERM grace inside _reap + SIGKILL + wait cap (2s) should stay tight.
    assert elapsed < 6.0
    # Child is gone (no zombie): the runner has already waited.
    try:
        import psutil
        live = []
        for p in psutil.Process().children(recursive=True):
            try:
                if p.status() == "zombie":
                    continue
                if "time.sleep(60)" in " ".join(p.cmdline()):
                    live.append(p.pid)
            except (psutil.ZombieProcess, psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        assert live == [], live
    except ImportError:
        pass


@pytest.mark.asyncio
async def test_broken_pipe_both_directions_does_not_hang() -> None:
    """Client gone: stdin already EOF and every write raises EPIPE. serve() must exit."""

    class Epipe:
        def write(self, b):
            raise BrokenPipeError("epipe on write")

        def flush(self):
            raise BrokenPipeError("epipe on flush")

    async def factory():
        return _FakeCore(), {}

    s = AvroneMCPServer(core_factory=factory)
    reader = asyncio.StreamReader()
    # A request the server will try to answer, then the client is already gone.
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    reader.feed_eof()
    await asyncio.wait_for(s.serve(reader, Epipe()), 3)


@pytest.mark.asyncio
async def test_subprocess_closed_stdout_and_stdin_exits(tmp_path: Path) -> None:
    """A real mcp_server child whose stdin *and* stdout are closed must not hang."""
    env = {
        **os.environ,
        "AVRONE_LLM_MODE": "mock",
        "AVRONE_RUNNER": "local",
        "AVRONE_WORKSPACE": str(tmp_path / "ws"),
        "AVRONE_DATA_DIR": str(tmp_path / "data"),
        "AVRONE_MCP_CONFIG": str(tmp_path / "none.json"),
        "AVRONE_ENV_FILE": str(tmp_path / "none.env"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    (tmp_path / "ws").mkdir()
    (tmp_path / "data").mkdir()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "avrone.mcp_server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, cwd=str(tmp_path),
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    try:
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    # Close both directions.
    proc.stdin.close()
    # asyncio's stdout is a StreamReader; close the transport to drop the pipe.
    transp = getattr(proc, "_transport", None)
    if transp is not None:
        pipe = transp.get_pipe_transport(1)
        if pipe is not None:
            pipe.close()
    try:
        await asyncio.wait_for(proc.wait(), 8)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        pytest.fail("mcp_server hung after both pipes were closed")
    assert proc.returncode is not None
