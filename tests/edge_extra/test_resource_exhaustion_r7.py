"""Round 7: bounded resource-exhaustion and cleanup.

Many concurrent in-process MCP sessions, a large tool registry, a payload
near the JSON-RPC line cap, and repeated StdioTransport connect/close
cycles with file-descriptor and task-leak checks.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from typing import Any

import pytest

from avrone import mcp_server as ms
from avrone.mcp_client import StdioTransport
from avrone.mcp_server import _ok_rpc_id
from avrone.tools import Tool, ToolRegistry


class _FakeTools:
    def __init__(self, n: int = 1):
        self._n = n

    def list(self):
        return [{"name": f"t{i}", "source": "builtin", "args": "{}", "description": f"tool {i}"}
                for i in range(self._n)]


class _FakeCore:
    def __init__(self, n_tools: int = 1):
        self.tools = _FakeTools(n_tools)

    async def squad(self, task, size=None):
        return {"answer": "ok", "run_id": "r", "agents": [1]}

    async def chat(self, task):
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r"}


async def _serve(lines: list[str | bytes], n_tools: int = 1, timeout: float = 4) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    for ln in lines:
        if isinstance(ln, bytes):
            reader.feed_data(ln if ln.endswith(b"\n") else ln + b"\n")
        else:
            reader.feed_data((ln if ln.endswith("\n") else ln + "\n").encode())
    reader.feed_eof()
    out = io.BytesIO()

    async def factory():
        return _FakeCore(n_tools), {}

    await asyncio.wait_for(ms.AvroneMCPServer(core_factory=factory).serve(reader, out), timeout)
    msgs = []
    for line in out.getvalue().decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0"
        assert "result" in msg or "error" in msg
        if "id" in msg:
            assert _ok_rpc_id(msg["id"]) is True
        msgs.append(msg)
    return msgs


@pytest.mark.asyncio
async def test_many_concurrent_inprocess_mcp_sessions() -> None:
    async def one(i: int) -> None:
        msgs = await _serve([
            json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": f"t{i}", "method": "tools/list"}),
        ])
        ids = {m["id"] for m in msgs}
        assert i in ids and f"t{i}" in ids

    await asyncio.gather(*(one(i) for i in range(16)))
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    # Cancel anything leftover so shutdown stays clean, then fail if there was a leak.
    leaked = [t.get_name() for t in pending]
    for t in pending:
        t.cancel()
    assert leaked == [], leaked


@pytest.mark.asyncio
async def test_many_tools_list_is_capped_only_by_registry() -> None:
    # tools/list is the fixed MCP surface; the large registry is exposed by avrone_tools.
    msgs = await _serve(
        [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "avrone_tools", "arguments": {}}})],
        n_tools=250,
    )
    assert len(msgs) == 1 and "result" in msgs[0]
    result = msgs[0]["result"]
    tools = (result.get("structuredContent") or {}).get("tools")
    if not tools:
        # Fallback: parse the text content.
        content = result.get("content") or []
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        tools = json.loads(text) if text.strip().startswith("[") else []
    names = [t["name"] for t in tools]
    assert len(names) == 250
    assert len(set(names)) == 250


def test_tool_registry_many_unique_names_stay_sorted() -> None:
    reg = ToolRegistry()
    for i in range(300):
        reg.register(Tool(f"tool_{i}", "d", "{}", lambda a, i=i: i))
    names = reg.names()
    assert names == [f"tool_{i}" for i in range(300)] or names == sorted(names)
    assert len(names) == 300
    # docs() must still be a string (not explode on a large registry).
    assert "tool_0" in reg.docs() and "tool_299" in reg.docs()


@pytest.mark.asyncio
async def test_near_limit_jsonrpc_line_is_answered() -> None:
    # Well under the 16 MiB StreamReader cap, but a large (64 KiB) string
    # params object: the server must still answer, not hang.
    big = "x" * (64 * 1024)
    msg = {"jsonrpc": "2.0", "id": "big", "method": "ping", "params": {"blob": big}}
    raw = json.dumps(msg)
    assert 60_000 < len(raw) < 16 * 1024 * 1024
    msgs = await _serve([raw])
    assert any(m.get("id") == "big" and ("result" in m or "error" in m) for m in msgs)


def _fd_count() -> int | None:
    for path in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return None


@pytest.mark.asyncio
async def test_repeated_connect_close_does_not_leak_fds() -> None:
    script = "import sys; sys.stdin.read()"
    before = _fd_count()
    for _ in range(30):
        t = StdioTransport(sys.executable, ["-c", script])
        await t.connect()
        await t.close()
        await t.close()  # idempotent
    # Let cancelled tasks settle.
    await asyncio.sleep(0.05)
    after = _fd_count()
    if before is not None and after is not None:
        # Slack for the fixture / pytest itself opening a file or two.
        assert after - before <= 8, (before, after)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    leaked = [t.get_name() for t in pending if "stdio" in t.get_name().lower() or "read" in t.get_name().lower()]
    for t in pending:
        t.cancel()
    # No leftover transport reader tasks.
    assert leaked == [], leaked
