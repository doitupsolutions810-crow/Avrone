"""v0.29: one MCP client cannot start unlimited runs. Before: a batch of N avrone_squad calls started N
squads at once, and a task of any size went into every prompt. Now: AVRONE_MCP_MAX_INFLIGHT (default 4)
concurrent runs, then an isError 'busy' result; tasks over AVRONE_MAX_MESSAGE_CHARS refused."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from avrone import mcp_server as m


class Core:
    tools = SimpleNamespace(list=lambda: [])

    def __init__(self):
        self.running = 0
        self.peak = 0

    async def chat(self, t):
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.05)
        self.running -= 1
        return {"answer": "ok"}


class Out(io.BytesIO):
    def flush(self):
        pass


def batch(n, task="t"):
    return json.dumps([{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": "avrone_chat", "arguments": {"task": task}}} for i in range(n)])


def serve(lines):
    core = Core()

    async def fac():
        return core, {}

    async def go():
        s = m.AvroneMCPServer(fac)
        r = asyncio.StreamReader()
        for ln in lines:
            r.feed_data((ln + "\n").encode())
        r.feed_eof()
        o = Out()
        await s.serve(r, o)
        return [json.loads(x) for x in o.getvalue().decode().splitlines()], s
    out, s = asyncio.run(go())
    return out, core, s


def test_inflight_cap(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", "2")
    out, core, s = serve([batch(6)])
    busy = [o for o in out if o["result"]["isError"]]
    assert core.peak == 2 and len(busy) == 4 and len(out) == 6
    assert "AVRONE_MCP_MAX_INFLIGHT" in busy[0]["result"]["content"][0]["text"]
    assert s._inflight == 0  # counter released


def test_counter_released_on_failure(monkeypatch):
    async def boom(self, *a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(m.AvroneMCPServer, "_run", boom)
    out, _, s = serve([batch(3)])
    assert all(o["result"]["isError"] for o in out) and s._inflight == 0


def test_task_too_long(monkeypatch):
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", "50")
    out, core, _ = serve([batch(1, "x" * 51)])
    assert "task too long" in out[0]["result"]["content"][0]["text"] and core.peak == 0
    deep = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "avrone_deep", "arguments": {"task": "q", "first_answer": "a" * 51}}})
    out, _, _ = serve([deep])
    assert "task too long" in out[0]["result"]["content"][0]["text"]


@pytest.mark.parametrize("raw,expect", [(None, 4), ("x", 4), ("0", 1), ("9", 9), ("1000", 64)])
def test_parse(monkeypatch, raw, expect):
    if raw is None:
        monkeypatch.delenv("AVRONE_MCP_MAX_INFLIGHT", raising=False)
    else:
        monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", raw)
    assert m.max_inflight() == expect
