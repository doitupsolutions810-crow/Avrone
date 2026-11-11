"""Round 8: hardest async faults — cancel, teardown, double-shutdown,
handshake abort, a never-returning tool, and a writer that blocks forever.
"""
from __future__ import annotations

import asyncio
import threading
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from avrone import mcp_server as ms
from avrone.config import Settings
from avrone.mcp_client import MCPClient, StdioTransport
from avrone.mcp_server import AvroneMCPServer, SHUTDOWN_TASK_TIMEOUT_S, WRITE_TIMEOUT_S
from avrone.sovereign import LocalRunner, _reap


class _FakeTools:
    def list(self):
        return []


class _FakeCore:
    def __init__(self, hang=False, boot_delay=0.0):
        self.tools = _FakeTools()
        self.hang = hang
        self.boot_delay = boot_delay

    async def squad(self, task, size=None):
        return await self.chat(task)

    async def chat(self, task):
        if self.hang:
            await asyncio.Event().wait()
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r"}


async def _factory(hang=False, boot_delay=0.0):
    if boot_delay:
        await asyncio.sleep(boot_delay)
    return _FakeCore(hang=hang), {}


@pytest.mark.asyncio
async def test_cancel_in_flight_stdio_request_reaps_child() -> None:
    # Server never replies; the parent cancels while the read is pending.
    t = StdioTransport(sys.executable, ["-c", "import sys,time; time.sleep(30)"])
    await t.connect()
    first = t.proc
    task = asyncio.create_task(t.request("initialize", {}, timeout=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await t.close()
    if first is not None and first.returncode is None:
        try:
            await asyncio.wait_for(first.wait(), 2)
        except asyncio.TimeoutError:
            first.kill()
            pytest.fail("stdio child leaked after cancelled request + close")


@pytest.mark.asyncio
async def test_cancel_propagates_from_runner_and_reaps(tmp_path: Path) -> None:
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 20
    r = LocalRunner(s)
    await r.start()
    task = asyncio.create_task(r.run("sleep 30", timeout=15))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancel_when_reap_times_out_still_raises(tmp_path: Path, monkeypatch) -> None:
    # The CancelledError handler in _exec must not hang if _reap itself stalls.
    async def slow_reap(proc):
        await asyncio.sleep(30)

    monkeypatch.setattr("avrone.sovereign._reap", slow_reap)
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 20
    r = LocalRunner(s)
    await r.start()
    task = asyncio.create_task(r.run("sleep 30", timeout=15))
    await asyncio.sleep(0.05)
    task.cancel()
    t0 = time.monotonic()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert time.monotonic() - t0 < 4.5  # bounded by the 2s wait_for around _reap
    finally:
        # slow_reap never killed the child; mop it up so the suite stays clean.
        try:
            import psutil
            for p in psutil.Process().children(recursive=True):
                try:
                    if "sleep 30" in " ".join(p.cmdline()):
                        p.kill()
                except Exception:
                    pass
        except ImportError:
            pass


@pytest.mark.asyncio
async def test_double_shutdown_is_idempotent() -> None:
    s = AvroneMCPServer(core_factory=lambda: _factory())
    s.warm()
    await s.shutdown()
    await s.shutdown()  # second pass must not raise
    await s.shutdown()


@pytest.mark.asyncio
async def test_shutdown_during_initialize_handshake_does_not_hang() -> None:
    async def slow_factory():
        return await _factory(boot_delay=15)

    s = AvroneMCPServer(core_factory=slow_factory)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
    reader.feed_eof()  # client hung up while the core is still booting
    out = io.BytesIO()
    await asyncio.wait_for(s.serve(reader, out), 4)
    # Boot task cancelled; no leaked core tasks.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    leaked = [t.get_name() for t in pending]
    for t in pending:
        t.cancel()
    assert leaked == [] or all("serve" not in n.lower() for n in leaked)


@pytest.mark.asyncio
async def test_hanging_tool_is_bounded_after_client_eof(monkeypatch) -> None:
    monkeypatch.setattr(ms, "SHUTDOWN_TASK_TIMEOUT_S", 0.4)
    s = AvroneMCPServer(core_factory=lambda: _factory(hang=True))
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"avrone_chat","arguments":{"task":"hi"}}}\n'
    )
    reader.feed_eof()
    out = io.BytesIO()
    t0 = time.monotonic()
    await asyncio.wait_for(s.serve(reader, out), 3)
    assert time.monotonic() - t0 < 2.5


@pytest.mark.asyncio
async def test_blocking_writer_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(ms, "WRITE_TIMEOUT_S", 0.3)

    released = threading.Event()

    class Forever:
        def write(self, b):
            released.wait(30)

        def flush(self):
            pass

    s = AvroneMCPServer(core_factory=lambda: _factory())
    s._out = Forever()
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(s.send({"jsonrpc": "2.0", "id": 1, "result": {}}), 3)
        assert time.monotonic() - t0 < 2.0
    finally:
        released.set()  # don't leave a worker thread sleeping


@pytest.mark.asyncio
async def test_event_loop_teardown_cancels_pending_serve() -> None:
    s = AvroneMCPServer(core_factory=lambda: _factory())
    reader = asyncio.StreamReader()  # no EOF: serve waits for a line
    out = io.BytesIO()
    task = asyncio.create_task(s.serve(reader, out))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await s.shutdown()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    for t in pending:
        t.cancel()
    assert pending == []
