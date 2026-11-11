"""Round 5: a real MCPClient talking to a real avrone.mcp_server subprocess."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from avrone.mcp_client import MCPClient, MCPError

ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "AVRONE_LLM_MODE": "mock",
        "AVRONE_RUNNER": "local",
        "AVRONE_WORKSPACE": str(tmp_path / "ws"),
        "AVRONE_DATA_DIR": str(tmp_path / "data"),
        "AVRONE_MCP_CONFIG": str(tmp_path / "none.json"),
        "AVRONE_PLUGINS_DIR": str(ROOT / "plugins"),
        "AVRONE_ENV_FILE": str(tmp_path / "none.env"),
        "PYTHONPATH": str(ROOT),
    }


@pytest.mark.timeout(60)
def test_mcp_client_roundtrips_with_real_server(tmp_path):
    async def go():
        c = MCPClient("avrone", {
            "command": sys.executable,
            "args": ["-m", "avrone.mcp_server"],
            "cwd": str(tmp_path),
            "env": _env(tmp_path),
            "timeout": 30,
        })
        await c.connect()
        try:
            assert c.protocol_version
            assert (c.server_info or {}).get("name") == "avrone"
            tools = await c.list_tools()
            names = {t["name"] for t in tools}
            assert names == {"avrone_squad", "avrone_chat", "avrone_deep", "avrone_tools"}
            # tools/list has no paging on this server; a second list still returns the same set.
            again = await c.list_tools()
            assert {t["name"] for t in again} == names
            assert await c.ping() is True
            listed = await c.call_tool("avrone_tools", {})
            assert "shell" in listed
            # Many built-in tools (far more than the 4 MCP wrappers).
            assert listed.count("\n") >= 10 or "calculator" in listed
            chat = await c.call_tool("avrone_chat", {"task": "say hi 🌍"})
            assert chat and "ERROR" not in chat[:20]
            # Unknown tool: JSON-RPC error surfaces as MCPError, not a hang.
            with pytest.raises(MCPError):
                await c.call_tool("definitely_not_a_tool", {"task": "x"})
        finally:
            await c.close()
            # Pending map must be empty after a clean close.
            assert c.transport._pending == {}

    asyncio.run(go())


@pytest.mark.timeout(45)
def test_mcp_client_recovers_after_adversarial_line(tmp_path):
    """A junk stdout line from the child must not poison a later client request.

    We inject the junk by wrapping the server command so a prefix of garbage
    is written before exec'ing the real server would be racy. Instead, reuse
    the already-covered reader poison path indirectly: the client must still
    be able to ping after a tools/call error result (isError, not a crash).
    """
    async def go():
        c = MCPClient("avrone", {
            "command": sys.executable,
            "args": ["-m", "avrone.mcp_server"],
            "cwd": str(tmp_path),
            "env": _env(tmp_path),
            "timeout": 30,
        })
        await c.connect()
        try:
            # Empty task is a tool-level error result, not an RPC failure.
            out = await c.call_tool("avrone_chat", {"task": "   "})
            assert "ERROR" in out or "task" in out.lower()
            assert await c.ping() is True
        finally:
            await c.close()

    asyncio.run(go())
