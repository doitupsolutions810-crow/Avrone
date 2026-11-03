"""Round 35: MCP client against misbehaving servers. Streamable HTTP: keep-alive streams that never
answer, oversized bodies/events, server->client requests inside the stream, invalid session ids,
session expiry, SSE field parsing. stdio: oversized lines, deep/huge JSON, odd ids, a server dying
mid-write. Config and results: string args, junk timeouts, non-object results, colliding tool names."""
import asyncio
import json
import sys
import textwrap
import time

import httpx
import pytest

from avrone import mcp_client as mc
from avrone.mcp_client import (HTTPTransport, MCPClient, MCPError, MCPManager, StdioTransport,
                               _match_response, _unwrap, args_doc_from_schema, clamp_timeout,
                               coerce_args, coerce_str_map, format_content, load_config,
                               max_response_bytes)
from avrone.tools import ToolRegistry


def run(c):
    return asyncio.run(c)


def rpc(rid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": rid}
    m.update({"error": error} if error is not None else {"result": result if result is not None else {}})
    return m


def http_transport(handler, headers=None):
    t = HTTPTransport("https://mcp.invalid/mcp", headers or {})
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


SSE = {"content-type": "text/event-stream"}


# ================================================================ config coercion
@pytest.mark.parametrize("v,want", [
    (None, []), ("-y @mcp/server .", ["-y", "@mcp/server", "."]), ("'a b' c", ["a b", "c"]),
    ("unbalanced 'quote", ["unbalanced", "'quote"]), (["a", 1, None], ["a", "1"]), (("x",), ["x"]),
    (5, ["5"]), ("", []),
])
def test_coerce_args(v, want):
    assert coerce_args(v) == want


def test_string_args_not_split_into_chars():
    t = StdioTransport("npx", "-y srv")
    assert t.args == ["-y", "srv"]


@pytest.mark.parametrize("v,want", [(None, {}), ({"A": 1, "B": None}, {"A": "1", "B": ""})])
def test_coerce_str_map(v, want):
    assert coerce_str_map(v, "env") == want


@pytest.mark.parametrize("v", [["A=1"], "A=1", 3])
def test_coerce_str_map_refuses(v):
    with pytest.raises(MCPError, match="'headers' must be an object"):
        coerce_str_map(v, "headers")


def test_bad_headers_clear_error():
    with pytest.raises(MCPError, match="must be an object"):
        MCPClient("w", {"url": "https://x.invalid", "headers": ["Authorization: k"]})


@pytest.mark.parametrize("v,want", [(30, 30.0), ("45", 45.0), ("abc", 60.0), (None, 60.0), (float("nan"), 60.0),
                                    (float("inf"), 60.0), (0, 60.0), (-5, 60.0), (0.2, 1.0), (99999, 3600.0)])
def test_clamp_timeout(v, want):
    assert clamp_timeout(v) == want


def test_client_timeout_from_config_clamped():
    assert MCPClient("s", {"command": "x", "timeout": "abc"}).timeout == mc.clamp_timeout(mc.DEFAULT_TIMEOUT)
    assert MCPClient("s", {"command": "x", "timeout": 0}).timeout == mc.clamp_timeout(mc.DEFAULT_TIMEOUT)
    assert MCPClient("s", {"command": "x", "timeout": 7}).timeout == 7.0


@pytest.mark.parametrize("raw,want", [("", 32 * 1024 * 1024), ("1", 64 * 1024), ("abc", 32 * 1024 * 1024),
                                      ("100000", 100000), (str(10 ** 12), 1024 ** 3)])
def test_max_response_env(monkeypatch, raw, want):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", raw)
    assert max_response_bytes() == want


@pytest.mark.parametrize("body", ['{"mcpServers": []}', '{"mcpServers": "fs"}', '{"servers": 3}'])
def test_load_config_non_object_servers(tmp_path, body):
    p = tmp_path / "mcp.json"
    p.write_text(body)
    with pytest.raises(ValueError, match="must be an object"):
        load_config(p)


def test_manager_reports_bad_config(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"mcpServers": ["fs"]}')
    st = run(MCPManager(config_path=p).start(ToolRegistry()))
    assert st["_config"]["connected"] is False and "must be an object" in st["_config"]["error"]


# ================================================================ HTTP: deadline
def test_keepalive_stream_hits_total_deadline():
    async def forever():
        while True:
            yield b": keep-alive\n\n"
            await asyncio.sleep(0.05)

    t = http_transport(lambda r: httpx.Response(200, headers=SSE, content=forever()))
    start = time.monotonic()
    with pytest.raises(MCPError, match=r"timeout waiting for tools/call \(1s\)"):
        run(t.request("tools/call", {}, 1))
    assert time.monotonic() - start < 4


def test_progress_trickle_still_bounded():
    async def trickle():
        while True:
            yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            await asyncio.sleep(0.05)
    t = http_transport(lambda r: httpx.Response(200, headers=SSE, content=trickle()))
    with pytest.raises(MCPError, match="timeout"):
        run(t.request("x", None, 1))


def test_slow_but_answering_stream_ok():
    async def slow():
        yield b": hi\n\n"
        await asyncio.sleep(0.3)
        yield ("data: " + json.dumps(rpc(1, {"ok": 1})) + "\n\n").encode()
    t = http_transport(lambda r: httpx.Response(200, headers=SSE, content=slow()))
    assert run(t.request("x", None, 5)) == {"ok": 1}


# ================================================================ HTTP: size caps
def test_json_body_over_cap(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")
    big = json.dumps(rpc(1, {"x": "a" * 70000}))
    t = http_transport(lambda r: httpx.Response(200, text=big, headers={"content-type": "application/json"}))
    with pytest.raises(MCPError, match="larger than 65536 bytes"):
        run(t.request("x", None, 5))


def test_json_body_under_cap(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")
    t = http_transport(lambda r: httpx.Response(200, json=rpc(1, {"x": "a" * 60000})))
    assert len(run(t.request("x", None, 5))["x"]) == 60000


def test_sse_event_over_cap(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")

    async def endless_data():
        while True:
            yield b"data: " + b"z" * 8192 + b"\n"
    t = http_transport(lambda r: httpx.Response(200, headers=SSE, content=endless_data()))
    with pytest.raises(MCPError, match="event larger than 65536"):
        run(t.request("x", None, 10))


def test_sse_cap_is_per_event(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")
    noise = "".join(f"data: {'n' * 40000}\n\n" for _ in range(5))  # 200 KB total, 40 KB per event
    body = noise + "data: " + json.dumps(rpc(1, {"ok": True})) + "\n\n"
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"ok": True}


def test_error_body_read_is_capped():
    async def huge():
        for _ in range(10000):
            yield b"E" * 1024
    t = http_transport(lambda r: httpx.Response(502, content=huge()))
    with pytest.raises(MCPError) as ei:
        run(t.request("x", None, 5))
    assert ei.value.code == 502 and str(ei.value) == "HTTP 502: " + "E" * 300


# ================================================================ HTTP: SSE field parsing
def test_sse_data_without_space_and_bare_data_line():
    body = 'data:{"jsonrpc":"2.0",\n' + "data\n" + 'data:  "id":1,"result":{"s":1}}\n\n'
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"s": 1}


def test_sse_comment_with_data_word_ignored():
    body = ': data: {"id":1,"result":{"fake":1}}\n\n' + f"data: {json.dumps(rpc(1, {'real': 1}))}\n\n"
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"real": 1}


def test_sse_data_prefix_lookalikes_ignored():
    body = ("database: nope\n" "data-x: nope\n"
            f"data: {json.dumps(rpc(1, {'ok': 1}))}\n\n")
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"ok": 1}


def test_sse_crlf_lines():
    body = f"event: message\r\ndata: {json.dumps(rpc(1, {'crlf': 1}))}\r\n\r\n"
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"crlf": 1}


def test_sse_id_and_retry_fields_ignored():
    body = f"id: 7\nretry: 1000\ndata: {json.dumps(rpc(1, {'ok': 2}))}\n\n"
    t = http_transport(lambda r: httpx.Response(200, text=body, headers=SSE))
    assert run(t.request("x", None, 5)) == {"ok": 2}


# ================================================================ HTTP: server->client requests
def test_ping_inside_stream_is_answered():
    posts = []

    def handler(r):
        body = json.loads(r.content)
        posts.append(body)
        if body.get("method") == "tools/call":
            sse = (f"data: {json.dumps({'jsonrpc': '2.0', 'id': 'srv-1', 'method': 'ping'})}\n\n"
                   f"data: {json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'sampling/createMessage'})}\n\n"
                   f"data: {json.dumps(rpc(1, {'done': 1}))}\n\n")
            return httpx.Response(200, text=sse, headers=SSE)
        return httpx.Response(202)
    t = http_transport(handler)
    assert run(t.request("tools/call", {}, 5)) == {"done": 1}
    replies = [p for p in posts if "method" not in p]
    assert {"jsonrpc": "2.0", "id": "srv-1", "result": {}} in replies
    assert any(p["id"] == 9 and p["error"]["code"] == -32601 for p in replies)


def test_server_request_reply_failure_does_not_break_call():
    def handler(r):
        body = json.loads(r.content)
        if "method" not in body:
            raise httpx.ConnectError("reply path down", request=r)
        sse = (f"data: {json.dumps({'jsonrpc': '2.0', 'id': 5, 'method': 'ping'})}\n\n"
               f"data: {json.dumps(rpc(1, {'ok': 1}))}\n\n")
        return httpx.Response(200, text=sse, headers=SSE)
    assert run(http_transport(handler).request("x", None, 5)) == {"ok": 1}


@pytest.mark.parametrize("msg", [{"jsonrpc": "2.0", "id": [1], "method": "ping"},
                                 {"jsonrpc": "2.0", "id": True, "method": "ping"},
                                 {"jsonrpc": "2.0", "method": "ping"},
                                 {"jsonrpc": "2.0", "id": 3, "method": 7}])
def test_odd_server_requests_not_answered(msg):
    posts = []

    def handler(r):
        body = json.loads(r.content)
        posts.append(body)
        sse = f"data: {json.dumps(msg)}\n\ndata: {json.dumps(rpc(1))}\n\n"
        return httpx.Response(200, text=sse, headers=SSE)
    run(http_transport(handler).request("x", None, 5))
    assert len(posts) == 1


# ================================================================ HTTP: session ids
@pytest.mark.parametrize("sid", ["bad id", "é-unicode", "x" * 1025, "tab\there"])
def test_invalid_session_id_ignored(sid, caplog):
    seen = []

    def handler(r):
        seen.append(r.headers.get("mcp-session-id"))
        return httpx.Response(200, json=rpc(json.loads(r.content)["id"]),
                              headers=[(b"Mcp-Session-Id", sid.encode("utf-8"))])
    t = http_transport(handler)

    async def go():
        await t.request("a", None, 5)
        await t.request("b", None, 5)  # used to fail: httpx refuses the header we echoed back
    run(go())
    assert t.session_id is None and seen == [None, None]
    assert "invalid Mcp-Session-Id" in caplog.text


def test_valid_session_id_kept():
    t = http_transport(lambda r: httpx.Response(200, json=rpc(1), headers={"Mcp-Session-Id": "abc-123_~!"}))
    run(t.request("a", None, 5))
    assert t.session_id == "abc-123_~!"


def test_session_expiry_reinitializes_once():
    state = {"session": "S1", "inits": 0}

    def handler(r):
        body = json.loads(r.content) if r.content else {}
        if r.method == "DELETE":
            return httpx.Response(200)
        if body.get("id") is None:
            return httpx.Response(202)
        if body["method"] == "initialize":
            state["inits"] += 1
            state["session"] = f"S{state['inits']}"
            return httpx.Response(200, json=rpc(body["id"], {"protocolVersion": "2025-06-18"}),
                                  headers={"Mcp-Session-Id": state["session"]})
        if r.headers.get("mcp-session-id") != state["session"] or state.get("expire"):
            state["expire"] = False
            return httpx.Response(404)
        return httpx.Response(200, json=rpc(body["id"], {"content": [{"type": "text", "text": "hi"}]}))

    async def go():
        c = MCPClient("web", {"url": "https://mcp.invalid/mcp"})
        c.transport = http_transport(handler)
        await c.initialize()
        state["expire"] = True
        out = await c.call_tool("t")
        return c, out
    c, out = run(go())
    assert out == "hi" and state["inits"] == 2 and c.transport.session_id == "S2"


def test_session_expiry_twice_gives_error():
    def handler(r):
        body = json.loads(r.content)
        if body.get("id") is None:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json=rpc(body["id"]), headers={"Mcp-Session-Id": "S"})
        return httpx.Response(404)

    async def go():
        c = MCPClient("web", {"url": "https://mcp.invalid/mcp"})
        c.transport = http_transport(handler)
        await c.initialize()
        await c.call_tool("t")
    with pytest.raises(MCPError, match="session expired"):
        run(go())


def test_plain_404_not_retried():
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(404, text="no endpoint")

    async def go():
        c = MCPClient("web", {"url": "https://mcp.invalid/mcp"})
        c.transport = http_transport(handler)
        await c.call_tool("t")
    with pytest.raises(MCPError, match="HTTP 404"):
        run(go())
    assert len(calls) == 1


# ================================================================ JSON edge payloads
def test_match_response_deep_nesting():
    assert _match_response("[" * 100000 + "]" * 100000, 1) is None


def test_match_response_huge_int():
    assert _match_response('{"id": 1, "result": ' + "9" * 5000 + "}", 1) is None


@pytest.mark.parametrize("err,code", [({"message": "m", "code": "E1"}, None), ({"message": "m", "code": True}, None),
                                      ({"message": "m", "code": -32000}, -32000), ({"message": "x" * 5000}, None)])
def test_unwrap_code_types(err, code):
    with pytest.raises(MCPError) as ei:
        _unwrap({"error": err})
    assert ei.value.code == code and len(str(ei.value)) <= 2000


# ================================================================ client: malformed results
class Scripted(mc._Transport):
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def request(self, method, params, timeout):
        self.calls.append((method, params))
        a = self.answers.get(method)
        return a(params) if callable(a) else a

    async def notify(self, method, params=None):
        pass


def client_with(answers):
    c = MCPClient("s", {"command": "x"})
    c.transport = Scripted(answers)
    return c


@pytest.mark.parametrize("res", [["not", "an", "object"], "string", 42, None])
def test_initialize_non_object_result(res):
    c = client_with({"initialize": res})
    run(c.initialize())
    assert c.protocol_version == mc.PROTOCOL_VERSION and c.server_info == {} and c.capabilities == {}


@pytest.mark.parametrize("ver", [["2025-06-18"], 2025, "2025\r\nX: y", "", "v" * 65])
def test_initialize_bad_protocol_version(ver):
    c = MCPClient("w", {"url": "https://x.invalid"})
    c.transport = Scripted({"initialize": {"protocolVersion": ver, "serverInfo": "junk", "capabilities": [1]}})
    c.transport.protocol_version = None
    run(c.initialize())
    assert c.protocol_version == mc.PROTOCOL_VERSION and c.server_info == {} and c.capabilities == {}


@pytest.mark.parametrize("res", [[1, 2], "tools", {"tools": None}, {"tools": "x"}, {"tools": {"name": "a"}}])
def test_list_tools_malformed(res):
    assert run(client_with({"tools/list": res}).list_tools()) == []


def test_list_tools_non_string_names_skipped():
    c = client_with({"tools/list": {"tools": [{"name": 42}, {"name": ["x"]}, {"name": "  "}, {"name": "ok"}, "junk"]}})
    assert [t["name"] for t in run(c.list_tools())] == ["ok"]


def test_list_tools_cursor_cycle_stops():
    pages = {None: ("a", "c1"), "c1": ("b", "c2"), "c2": ("c", "c1")}

    def ans(params):
        name, nxt = pages[(params or {}).get("cursor")]
        return {"tools": [{"name": name}], "nextCursor": nxt}
    c = client_with({"tools/list": ans})
    assert [t["name"] for t in run(c.list_tools())] == ["a", "b", "c"]
    assert len(c.transport.calls) == 3


def test_list_tools_distinct_cursors_still_capped_at_100():
    c = client_with({"tools/list": lambda p: {"tools": [], "nextCursor": f"c{len(c.transport.calls)}"}})
    run(c.list_tools())
    assert len(c.transport.calls) == 100


@pytest.mark.parametrize("cur", [5, ["c"], {"c": 1}, ""])
def test_list_tools_non_string_cursor_stops(cur):
    c = client_with({"tools/list": {"tools": [{"name": "a"}], "nextCursor": cur}})
    run(c.list_tools())
    assert len(c.transport.calls) == 1


@pytest.mark.parametrize("res,want", [
    ([1, 2], "malformed result [1, 2]"), ("text", 'malformed result "text"'), (None, "(no content)"),
    ({"content": "plain string"}, "plain string"), ({"content": {"type": "text", "text": "one"}}, "one"),
    ({"content": 5}, "(no content)"),
    ({"content": [{"type": "resource", "resource": "junk"}]}, "[resource:  (binary) omitted]"),
])
def test_call_tool_malformed_results(res, want):
    assert want in run(client_with({"tools/call": res}).call_tool("t"))


def test_format_content_string_not_one_char_per_line():
    assert format_content({"content": "abc"}) == "abc"


# ================================================================ args doc
@pytest.mark.parametrize("schema,want", [
    ({"properties": [1, 2]}, "{}"),
    ({"properties": {"path": {"type": "string"}}, "required": "path"}, '{"path": str}'),
    ({"properties": {"p": {"type": "string"}}, "required": {"p": 1}}, '{"p"?: str}'),
    ({"properties": {"m": {"enum": "abc"}}}, '{"m"?: any}'),
    ({"properties": {"m": {"enum": []}}}, '{"m"?: any}'),
    ({"properties": {"m": {"enum": [{"k": 1}, None]}}}, '{"m"?: {"k": 1}|null}'),
])
def test_args_doc_junk_schemas(schema, want):
    assert args_doc_from_schema(schema) == want


# ================================================================ manager: name collisions
class FakeClient:
    tools_by_server: dict = {}

    def __init__(self, name, cfg, timeout=30):
        self.name, self.kind, self.timeout = name, "stdio", 1.0
        self.server_info, self.protocol_version = {}, "2025-06-18"

    async def connect(self):
        pass

    async def list_tools(self):
        return self.tools_by_server[self.name]

    async def call_tool(self, tool, args):
        return f"{self.name}:{tool}"

    async def close(self):
        pass


def test_colliding_tool_names_get_suffix(monkeypatch):
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    FakeClient.tools_by_server = {"s": [{"name": "a b"}, {"name": "a_b"}, {"name": "a?b"}]}
    reg = ToolRegistry()
    m = MCPManager(servers={"s": {"command": "x"}})
    st = run(m.start(reg))
    assert st["s"]["tools"] == 3
    names = sorted(n for n in m._registered)
    assert names == ["mcp__s__a_b", "mcp__s__a_b_2", "mcp__s__a_b_3"]
    outs = sorted(run(reg.get(n).fn(None, {})) for n in names)
    assert outs == ["s:a b", "s:a?b", "s:a_b"]  # every remote tool still reachable


def test_servers_that_sanitize_alike(monkeypatch):
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    FakeClient.tools_by_server = {"my srv": [{"name": "t"}], "my_srv": [{"name": "t"}]}
    reg = ToolRegistry()
    m = MCPManager(servers={"my srv": {"command": "x"}, "my_srv": {"command": "x"}})
    run(m.start(reg))
    assert sorted(m._registered) == ["mcp__my_srv__t", "mcp__my_srv__t_2"]
    run(m.stop())
    assert all(reg.get(n) is None for n in ["mcp__my_srv__t", "mcp__my_srv__t_2"])


def test_long_tool_name_trimmed(monkeypatch):
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    FakeClient.tools_by_server = {"s": [{"name": "x" * 500}]}
    m = MCPManager(servers={"s": {"command": "x"}})
    run(m.start(ToolRegistry()))
    assert len(m._registered[0]) <= len("mcp__s__") + 64


def test_non_string_description(monkeypatch):
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    FakeClient.tools_by_server = {"s": [{"name": "t", "description": {"en": "d"}}]}
    reg = ToolRegistry()
    m = MCPManager(servers={"s": {"command": "x"}})
    assert run(m.start(reg))["s"]["tools"] == 1


# ================================================================ stdio: real subprocess server
SERVER = textwrap.dedent(r'''
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m or "method" not in m:
            continue
        meth, rid = m["method"], m["id"]
        out = sys.stdout
        if meth == "big":
            out.write('{"jsonrpc":"2.0","id":%d,"result":{"x":"%s"}}\n' % (rid, "a" * 200000))
        elif meth == "deep":
            out.write("[" * 200000 + "]" * 200000 + "\n")
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"deep": "ok"}}) + "\n")
        elif meth == "oddids":
            for bad in ([1], {"a": 1}, True, None):
                out.write(json.dumps({"jsonrpc": "2.0", "id": bad, "result": 1}) + "\n")
            out.write(json.dumps({"jsonrpc": "2.0", "id": [2], "method": "ping"}) + "\n")
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"ids": "ok"}}) + "\n")
        elif meth == "hugeint":
            out.write('{"jsonrpc":"2.0","id":%d,"result":%s}\n' % (rid, "9" * 6000))
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"int": "ok"}}) + "\n")
        elif meth == "exit":
            sys.exit(0)
        else:
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"echo": meth}}) + "\n")
        out.flush()
''')


@pytest.fixture()
def server_py(tmp_path):
    p = tmp_path / "srv.py"
    p.write_text(SERVER)
    return str(p)


def test_stdio_oversized_line_fails_request_keeps_connection(server_py, monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")

    async def go():
        t = StdioTransport(sys.executable, [server_py])
        await t.connect()
        try:
            with pytest.raises(MCPError, match="over 65536 bytes"):
                await t.request("big", None, 10)
            return await t.request("after", None, 10)  # used to hang: the reader had died
        finally:
            await t.close()
    assert run(go()) == {"echo": "after"}


def test_stdio_deep_json_line_skipped(server_py):
    async def go():
        t = StdioTransport(sys.executable, [server_py])
        await t.connect()
        try:
            return await t.request("deep", None, 10)
        finally:
            await t.close()
    assert run(go()) == {"deep": "ok"}


def test_stdio_odd_ids_dont_kill_reader(server_py):
    async def go():
        t = StdioTransport(sys.executable, [server_py])
        await t.connect()
        try:
            a = await t.request("oddids", None, 10)
            b = await t.request("again", None, 10)
            return a, b
        finally:
            await t.close()
    assert run(go()) == ({"ids": "ok"}, {"echo": "again"})


def test_stdio_huge_int_line_skipped(server_py):
    async def go():
        t = StdioTransport(sys.executable, [server_py])
        await t.connect()
        try:
            return await t.request("hugeint", None, 10)
        finally:
            await t.close()
    assert run(go()) == {"int": "ok"}


@pytest.mark.parametrize("exc", [BrokenPipeError, ConnectionResetError, OSError])
def test_stdio_write_race_is_mcp_error(exc):
    class Stdin:
        def write(self, data):
            raise exc("pipe closed")

        async def drain(self):
            pass

    class Proc:
        returncode = None  # exited, but not reaped yet
        stdin = Stdin()
    t = StdioTransport("x")
    t.proc = Proc()
    with pytest.raises(MCPError, match="not running"):
        run(t.request("x", None, 2))
    assert t._pending == {}


def test_stdio_drain_race_is_mcp_error():
    class Stdin:
        def write(self, data):
            pass

        async def drain(self):
            raise ConnectionResetError("reset")

    class Proc:
        returncode = None
        stdin = Stdin()
    t = StdioTransport("x")
    t.proc = Proc()
    with pytest.raises(MCPError, match="not running"):
        run(t.notify("notifications/x"))


def test_stdio_long_line_then_many_normal(server_py, monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", "65536")

    async def go():
        t = StdioTransport(sys.executable, [server_py])
        await t.connect()
        try:
            out = []
            for i in range(3):
                with pytest.raises(MCPError, match="over 65536"):
                    await t.request("big", None, 10)
                out.append(await t.request(f"m{i}", None, 10))
            return out
        finally:
            await t.close()
    assert run(go()) == [{"echo": "m0"}, {"echo": "m1"}, {"echo": "m2"}]


def test_stdio_dispatch_unhashable_id_direct():
    t = StdioTransport("x")
    run(t._dispatch({"id": {"a": 1}, "result": 1}))
    run(t._dispatch({"id": [1], "result": 1}))
    run(t._dispatch("not a dict"))
