"""Round 5 JSON-RPC edges: jsonrpc version, id shapes, tools/list cursor; raise vs error."""
from __future__ import annotations

import asyncio
import io
import json

from avrone import mcp_server as ms


class FakeTools:
    def list(self):
        return [{"name": "shell", "source": "builtin", "args": "{}", "description": "run"}]


class FakeCore:
    def __init__(self, fail=False):
        self.tools = FakeTools()
        self.fail = fail

    async def squad(self, task, size=None):
        if self.fail:
            raise RuntimeError("model down")
        return {"answer": f"squad:{task}", "run_id": "r1", "agents": [1]}

    async def chat(self, task):
        if self.fail:
            raise RuntimeError("model down")
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r2"}


def _server(core):
    async def factory():
        return core, {}
    return ms.AvroneMCPServer(core_factory=factory)


def _serve(lines, core=None):
    async def go():
        s = _server(core or FakeCore())
        reader = asyncio.StreamReader()
        for ln in lines:
            reader.feed_data((ln if isinstance(ln, str) else json.dumps(ln)).encode() + b"\n")
        reader.feed_eof()
        out = io.BytesIO()
        await s.serve(reader, out)
        return [json.loads(x) for x in out.getvalue().decode().splitlines() if x]
    return asyncio.run(go())


def test_jsonrpc_version_missing_or_wrong_is_invalid_request():
    msgs = _serve([
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 2, "method": "ping"},
        {"jsonrpc": 2.0, "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ])
    by = {m["id"]: m for m in msgs}
    assert by[1]["error"]["code"] == -32600
    assert by[2]["error"]["code"] == -32600
    assert by[3]["error"]["code"] == -32600
    assert "result" in by[4]


def test_id_empty_string_and_huge_int_are_echoed():
    huge = 10**50
    msgs = _serve([
        {"jsonrpc": "2.0", "id": "", "method": "ping"},
        {"jsonrpc": "2.0", "id": huge, "method": "ping"},
    ])
    ids = {m["id"] for m in msgs}
    assert "" in ids and huge in ids
    assert all("result" in m for m in msgs)


def test_id_object_array_and_nan_are_rejected():
    msgs = _serve([
        {"jsonrpc": "2.0", "id": {"nested": {"a": 1}}, "method": "ping"},
        {"jsonrpc": "2.0", "id": [1, 2, 3], "method": "ping"},
        {"jsonrpc": "2.0", "id": float("nan"), "method": "ping"},
        {"jsonrpc": "2.0", "id": 9, "method": "ping"},
    ])
    # Bad ids are answered with id=null so the body stays encodable.
    assert any(m.get("id") is None and m.get("error", {}).get("code") == -32600 for m in msgs)
    assert any(m.get("id") == 9 and "result" in m for m in msgs)


def test_tools_list_ignores_cursor_and_has_no_next(client_core=None):
    msgs = _serve([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": "page-2"}},
    ])
    assert len(msgs) == 1 and "result" in msgs[0]
    names = {t["name"] for t in msgs[0]["result"]["tools"]}
    assert names == {"avrone_squad", "avrone_chat", "avrone_deep", "avrone_tools"}
    assert "nextCursor" not in msgs[0]["result"]


def test_tool_that_raises_is_returned_as_iserror_not_rpc_crash():
    msgs = _serve([{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "avrone_chat", "arguments": {"task": "x"}},
    }], core=FakeCore(fail=True))
    assert len(msgs) == 1
    # call_tool catches the exception and returns an isError tool result, not a -32603 at the RPC layer.
    body = msgs[0]
    assert "result" in body
    assert body["result"]["isError"] is True
    assert "RuntimeError" in body["result"]["content"][0]["text"]


def test_slow_writer_backpressure_still_completes():
    """send() writes are serialized; a slow client drain must not drop or hang later pings."""
    import time

    class Slow(io.BytesIO):
        def write(self, b):
            time.sleep(0.01)
            return super().write(b)

    async def go():
        s = _server(FakeCore())
        reader = asyncio.StreamReader()
        for i in range(20):
            reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}).encode() + b"\n")
        reader.feed_eof()
        out = Slow()
        await asyncio.wait_for(s.serve(reader, out), timeout=8)
        msgs = [json.loads(x) for x in out.getvalue().decode().splitlines() if x]
        assert {m["id"] for m in msgs} == set(range(20))
        assert all("result" in m for m in msgs)

    asyncio.run(go())
