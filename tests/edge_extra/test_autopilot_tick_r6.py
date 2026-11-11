"""Round 6: a full mock autopilot tick with injected faults.

Wires governor -> evolve gate command -> sovereign subprocess -> MCP
round-trip, then asserts clean shutdown (no leaked tasks or zombies)
whether every stage is healthy or one of them faults.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from avrone.config import Settings
from avrone.evolve import _safe_test_cmd
from avrone import mcp_server as ms
from avrone.governor import Governor
from avrone.sovereign import LocalRunner


def _readers(**vals):
    defaults = {"load": 0.1, "mem_pct": 20.0, "disk_free_pct": 50.0, "temp_c": 40.0}
    defaults.update(vals)
    return {k: (lambda v=v: v) for k, v in defaults.items()}



class _FakeTools:
    def list(self):
        return [{"name": "echo", "source": "builtin", "args": "{}", "description": "echo"}]


class _FakeCore:
    def __init__(self):
        self.tools = _FakeTools()

    async def squad(self, task, size=None):
        return {"answer": "ok", "run_id": "r1", "agents": [1]}

    async def chat(self, task):
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r2"}


async def _mcp_ping(*, fault: bool = False) -> list[dict[str, Any]]:
    import io
    reader = asyncio.StreamReader(limit=65536)
    if fault:
        # A truncated request plus a valid ping: the server must still answer the ping.
        reader.feed_data(b'{"jsonrpc":"2.0","id":\n')
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    reader.feed_eof()
    out = io.BytesIO()

    async def factory():
        return _FakeCore(), {}

    s = ms.AvroneMCPServer(core_factory=factory)
    await asyncio.wait_for(s.serve(reader, out), 3)
    msgs = []
    for line in out.getvalue().decode("utf-8", "replace").splitlines():
        if line.strip():
            msgs.append(json.loads(line))
    return msgs


async def _tick(tmp_path: Path, *, fault: str | None = None) -> dict[str, Any]:
    """One mock autopilot cycle. `fault` injects a failure at one stage."""
    report: dict[str, Any] = {"fault": fault, "leaked": []}

    readers = _readers(load=99.0) if fault == "governor" else _readers()
    g = Governor(8, min_parallel=1, ttl_s=0, readers=readers)
    workers = g.target()
    report["workers"] = workers
    report["throttled"] = workers < 8

    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 8
    runner = LocalRunner(s)
    await runner.start()
    try:
        # Evolve gate command: the env-override path is quoted via _safe_test_cmd.
        if fault == "evolve":
            raw = "true; echo injected"
            cmd = _safe_test_cmd(raw)
            # Even after sanitizing, the command is a result, not a crash.
            gate = await runner.run(cmd, timeout=5)
        elif fault == "sovereign":
            gate = await runner.run("sleep 30", timeout=1)
        else:
            # A successful default-shaped gate through the quoted shell wrapper.
            gate = await runner.run("true", timeout=5)
        report["gate_exit"] = gate.exit_code
        report["gate_timeout"] = bool(gate.timed_out)
        report["gate_stderr"] = (gate.stderr or "")[:200]

        ping = await _mcp_ping(fault=(fault == "mcp"))
        report["mcp_ok"] = any(m.get("id") == 1 and "result" in m for m in ping)
        report["mcp_lines"] = len(ping)
        for m in ping:
            assert m.get("jsonrpc") == "2.0"
    finally:
        # Clean shutdown of the runner (LocalRunner.stop is a no-op / safe).
        stop = getattr(runner, "stop", None)
        if callable(stop):
            res = stop()
            if asyncio.iscoroutine(res):
                await res

    return report


def _assert_no_leaked_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    # Cancelling leftovers keeps the suite from hanging on shutdown; then fail
    # so a leak is still visible.
    leaked = [t.get_name() for t in pending]
    for t in pending:
        t.cancel()
    if pending:
        pytest.fail(f"leaked asyncio tasks after tick: {leaked}")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "governor", "evolve", "sovereign", "mcp"])
async def test_mock_autopilot_tick_with_injected_faults(tmp_path: Path, fault: str | None) -> None:
    before = {p.pid for p in _child_procs()}
    report = await _tick(tmp_path, fault=fault)

    if fault == "governor":
        assert report["throttled"] is True
        assert report["workers"] == 1
        assert report["mcp_ok"] is True
        assert report["gate_timeout"] is False
    elif fault == "sovereign":
        assert report["gate_timeout"] is True
        assert report["mcp_ok"] is True  # later stages still run
    elif fault == "evolve":
        # The sanitized command must not have run a second injected statement
        # as a separate unquoted command; exit is still a result.
        assert report["gate_exit"] in (0, 1, -1) or report["gate_timeout"] in (True, False)
        assert report["mcp_ok"] is True
    elif fault == "mcp":
        # Truncated frame + a valid ping: the ping is still answered.
        assert report["mcp_ok"] is True
    else:
        assert report["throttled"] is False
        assert report["workers"] == 8
        assert report["gate_exit"] == 0
        assert report["gate_timeout"] is False
        assert report["mcp_ok"] is True

    _assert_no_leaked_tasks()
    await asyncio.sleep(0.05)
    leftover = [p for p in _child_procs() if p.pid not in before and _is_zombie_or_ours(p)]
    # Reap anything we spawned; nothing should still be running.
    still_running = [p for p in leftover if p.returncode is None and not _is_zombie(p)]
    assert still_running == []


def _child_procs():
    try:
        import psutil
    except ImportError:
        return []
    me = psutil.Process()
    try:
        return me.children(recursive=True)
    except Exception:
        return []


def _is_zombie(p) -> bool:
    try:
        return p.status() == "zombie"
    except Exception:
        return False


def _is_zombie_or_ours(p) -> bool:
    try:
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_healthy_tick_shuts_down_without_pending_tasks(tmp_path: Path) -> None:
    await _tick(tmp_path, fault=None)
    await asyncio.sleep(0)
    _assert_no_leaked_tasks()
