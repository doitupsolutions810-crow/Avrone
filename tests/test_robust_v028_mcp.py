"""v0.28: MCP server fuzzed with malformed JSON-RPC. Before: non-object params/arguments and a
non-string method leaked AttributeError as -32603, a dict task ran as its repr, an empty batch got
no answer, and one line over the reader limit killed the whole server."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from avrone import mcp_server as m


class Core:
    tools = SimpleNamespace(list=lambda: [])
    ran: list = []

    async def squad(self, t, n=None, **k):
        Core.ran.append(t)
        return {"answer": "ok", "run_id": "r", "agents": []}

    async def chat(self, t):
        Core.ran.append(t)
        return {"answer": "c"}


async def fac():
    return Core(), {}


class Out(io.BytesIO):
    def flush(self):
        pass


def serve(lines, limit=2 ** 16):
    async def go():
        s = m.AvroneMCPServer(fac)
        r = asyncio.StreamReader(limit=limit)
        for ln in lines:
            r.feed_data(ln if isinstance(ln, bytes) else (ln + "\n").encode())
        r.feed_eof()
        o = Out()
        await s.serve(r, o)
        return [json.loads(x) for x in o.getvalue().decode().splitlines()]
    return asyncio.run(go())


PING = '{"jsonrpc":"2.0","id":99,"method":"ping"}'


@pytest.mark.parametrize("req,code,frag", [
    ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}', -32602, "params must be an object"),
    ('{"jsonrpc":"2.0","id":1,"method":5}', -32600, "method must be a string"),
    ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"avrone_chat","arguments":[1]}}', -32602,
     "arguments must be an object"),
    ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":"x"}', -32602, "params must be an object"),
    ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"' + "z" * 5000 + '"}}', -32602, "Unknown tool"),
])
def test_malformed_requests_get_spec_errors(req, code, frag):
    out = serve([req, PING])
    err = out[0]["error"]
    assert err["code"] == code and frag in err["message"] and "AttributeError" not in err["message"]
    assert len(err["message"]) < 200 and out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


def test_non_string_task_refused_not_run():
    Core.ran = []
    out = serve(['{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"avrone_chat",'
                 '"arguments":{"task":{"a":1}}}}'])
    assert out[0]["result"]["isError"] and "must be a string" in out[0]["result"]["content"][0]["text"]
    assert Core.ran == []


def test_null_params_and_arguments_ok():
    out = serve(['{"jsonrpc":"2.0","id":1,"method":"ping","params":null}',
                 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"avrone_tools","arguments":null}}'])
    assert {o["id"] for o in out} == {1, 2} and all("result" in o for o in out)


def test_empty_batch_is_invalid_request():
    out = serve(["[]", PING])
    assert out[0]["error"]["code"] == -32600 and out[0]["id"] is None and out[1]["id"] == 99


def test_oversized_line_does_not_kill_server():
    big = '{"jsonrpc":"2.0","id":7,"method":"ping","x":"' + "x" * 5000 + '"}'
    out = serve([big, PING], limit=1024)
    assert out[0]["error"]["code"] == -32600 and "size limit" in out[0]["error"]["message"]
    assert out[-1]["id"] == 99


def test_oversized_line_without_newline_yet():
    big = ('{"jsonrpc":"2.0","id":7,"method":"ping","x":"' + "x" * 5000).encode()
    out = serve([big, b'"}\n', PING], limit=1024)
    assert out[0]["error"]["code"] == -32600 and out[-1]["id"] == 99
