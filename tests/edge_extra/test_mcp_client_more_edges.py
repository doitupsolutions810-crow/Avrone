"""Round 4 mcp_client edges: poison lines, partial frames, duplicate/unknown ids, load_config paths."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from avrone.mcp_client import MCPError, StdioTransport, load_config

from tests.test_mcp_client_edges import FakeProc, install_proc, run


def test_deeply_nested_stdout_line_does_not_kill_pending(monkeypatch):
    """A RecursionError from json.loads used to fail every in-flight future with
    'process exited' even though the child was still alive."""
    proc = FakeProc(lambda m: [{"jsonrpc": "2.0", "id": m["id"], "result": {"ok": True}}]
                    if m.get("id") is not None else [])
    install_proc(monkeypatch, proc)

    async def go():
        t = StdioTransport("nested")
        await t.connect()
        try:
            poison = "[" * 2000 + "]" * 2000 + "\n"
            t.proc.stdout.feed_data(poison.encode())
            await asyncio.sleep(0.05)
            # The reader must still be able to complete a later request.
            result = await t.request("ping", {}, timeout=2)
            assert result == {"ok": True}
            assert t._pending == {}
        finally:
            await t.close()

    run(go())


def test_partial_then_complete_frame_completes_request(monkeypatch):
    proc = FakeProc(lambda m: [])  # we complete the response ourselves, in chunks
    install_proc(monkeypatch, proc)

    async def go():
        t = StdioTransport("drip")
        await t.connect()
        try:
            task = asyncio.create_task(t.request("ping", {}, timeout=2))
            await asyncio.sleep(0.02)
            # id is 1 for the first request
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"pong": True}})
            mid = len(payload) // 2
            t.proc.stdout.feed_data(payload[:mid].encode())
            await asyncio.sleep(0.03)
            assert not task.done()
            t.proc.stdout.feed_data(payload[mid:].encode() + b"\n")
            result = await asyncio.wait_for(task, timeout=2)
            assert result == {"pong": True}
        finally:
            await t.close()

    run(go())


def test_duplicate_and_unknown_response_ids_are_ignored(monkeypatch):
    def respond(m):
        rid = m.get("id")
        if rid is None:
            return []
        body = {"jsonrpc": "2.0", "id": rid, "result": {"n": rid}}
        return [body, body, {"jsonrpc": "2.0", "id": 999999, "result": {"ghost": True}}]

    install_proc(monkeypatch, FakeProc(respond))

    async def go():
        t = StdioTransport("dup")
        await t.connect()
        try:
            a = await t.request("ping", {}, timeout=2)
            b = await t.request("ping", {}, timeout=2)
            assert a == {"n": 1} and b == {"n": 2}
            assert t._pending == {}
        finally:
            await t.close()

    run(go())


def test_stdout_close_mid_request_fails_pending_and_cleans_up(monkeypatch):
    install_proc(monkeypatch, FakeProc(lambda m: []))

    async def go():
        t = StdioTransport("eof")
        await t.connect()
        try:
            task = asyncio.create_task(t.request("tools/list", {}, timeout=2))
            await asyncio.sleep(0.02)
            t.proc.stdout.feed_eof()
            with pytest.raises(MCPError, match="exited"):
                await task
            assert t._pending == {}
        finally:
            await t.close()

    run(go())


def test_load_config_directory_and_broken_symlink_are_empty(tmp_path):
    d = tmp_path / "mcp.json"
    d.mkdir()
    assert load_config(d) == {}
    missing = tmp_path / "nope.json"
    link = tmp_path / "broken.json"
    link.symlink_to(missing)
    assert load_config(link) == {}


def test_load_config_symlink_to_valid_file(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"mcpServers": {"s": {"command": "x"}}}))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert load_config(link) == {"s": {"command": "x"}}


def test_load_config_unknown_keys_and_disabled_are_skipped(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "ok": {"command": "c", "timeout": 5, "unexpected": True},
            "off": {"command": "c", "disabled": True},
        },
        "extraTop": 1,
    }))
    assert load_config(p) == {"ok": {"command": "c", "timeout": 5, "unexpected": True}}
