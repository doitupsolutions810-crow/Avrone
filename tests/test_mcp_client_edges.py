"""Offline edge tests for avrone.mcp_client: stdio transport over a fake process, Streamable HTTP
over httpx.MockTransport, timeouts, junk/batch/partial responses, connection errors, session
handling, output caps, config loading and the manager's failure paths. No real network or
subprocesses."""
import asyncio
import json
import logging

import httpx
import pytest

from avrone import mcp_client as mc
from avrone.mcp_client import (HTTPTransport, MCPClient, MCPError, MCPManager, StdioTransport, _match_response,
                               _unwrap, args_doc_from_schema, format_content, load_config)
from avrone.tools import Tool, ToolRegistry


def run(c):
    return asyncio.run(c)


# ============================================================== fake stdio process
class FakeStdin:
    def __init__(self, proc):
        self.proc, self.sent, self.closed = proc, [], False

    def write(self, data):
        for line in data.decode().splitlines():
            msg = json.loads(line)
            self.sent.append(msg)
            for out in self.proc.responder(msg):
                self.proc.stdout.feed_data((out if isinstance(out, str) else json.dumps(out)).encode() + b"\n")

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, responder, wait_mode="ok"):
        self.stdout = self.stderr = None  # made inside the running loop by streams()
        self.stdin = FakeStdin(self)
        self.responder = responder
        self.returncode = None
        self.wait_mode = wait_mode
        self.terminated = self.killed = False

    def streams(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        return self

    async def wait(self):
        if self.wait_mode == "ok" or (self.wait_mode == "needs_kill" and self.killed) or \
                (self.wait_mode == "needs_term" and self.terminated):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            return 0
        raise RuntimeError("still running")

    def terminate(self):
        self.terminated = True
        if self.wait_mode == "term_gone":
            raise ProcessLookupError

    def kill(self):
        self.killed = True


def install_proc(monkeypatch, proc, seen=None):
    async def fake_exec(*argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        return proc.streams()
    monkeypatch.setattr(mc.asyncio, "create_subprocess_exec", fake_exec)


def server_responder(pages=1, tools_error=False):
    """A small, slightly noisy MCP server."""
    def respond(m):
        method, rid = m.get("method"), m.get("id")
        if rid is None:  # notification
            return []
        if method == "initialize":
            return ["", "not json at all", 42, [1, "x"],
                    {"jsonrpc": "2.0", "method": "notifications/message", "params": {}},
                    {"jsonrpc": "2.0", "id": 900, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 901, "method": "sampling/createMessage"},
                    {"jsonrpc": "2.0", "id": 12345, "result": {}},  # unknown id: dropped
                    {"jsonrpc": "2.0", "id": rid, "result": {
                        "protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"},
                        "capabilities": {"tools": {}}}}]
        if method == "tools/list":
            cur = (m.get("params") or {}).get("cursor")
            page = int(cur or 0)
            res = {"tools": [{"name": f"t{page}"}, {"nameless": True}, "junk"]}
            if page + 1 < pages:
                res["nextCursor"] = str(page + 1)
            return [[{"jsonrpc": "2.0", "id": rid, "result": res}]]  # batch form
        if method == "tools/call":
            if tools_error:
                return [{"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "kaput", "data": {"x": 1}}}]
            return [{"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "hi"}],
                                                              "isError": m["params"]["name"] == "bad"}}]
        if method == "ping":
            return [{"jsonrpc": "2.0", "id": rid, "result": {}}]
        return []
    return respond


def test_stdio_client_full_flow_with_noise(monkeypatch):
    proc = FakeProc(server_responder(pages=3))
    seen = []
    install_proc(monkeypatch, proc, seen)

    async def go():
        c = MCPClient("fs", {"command": "fake-mcp", "args": ["--x", 1], "env": {"A": 2}, "cwd": "/tmp", "timeout": 3})
        assert c.kind == "stdio" and c.timeout == 3.0
        await c.connect()
        tools = await c.list_tools()
        ok = await c.call_tool("good", None)
        bad = await c.call_tool("bad", {"a": 1})
        pong = await c.ping()
        await c.transport.notify("notifications/progress", {"p": 1})
        await c.close()
        return c, tools, ok, bad, pong

    c, tools, ok, bad, pong = run(go())
    argv, kw = seen[0]
    assert argv == ("fake-mcp", "--x", "1") and kw["env"]["A"] == "2" and kw["cwd"] == "/tmp"
    assert c.protocol_version == "2024-11-05" and c.server_info == {"name": "fake"}
    assert [t["name"] for t in tools] == ["t0", "t1", "t2"]
    assert ok == "hi" and bad == "ERROR (from MCP tool fs/bad): hi" and pong is True
    sent = proc.stdin.sent
    assert {"jsonrpc": "2.0", "id": 900, "result": {}} in sent
    assert any(m.get("id") == 901 and m["error"]["code"] == -32601 for m in sent)
    assert any(m.get("method") == "notifications/initialized" and "params" not in m for m in sent)
    assert any(m.get("method") == "ping" and "params" not in m for m in sent)
    assert any(m.get("method") == "notifications/progress" and m["params"] == {"p": 1} for m in sent)
    assert proc.stdin.closed and not c.transport._pending


def test_stdio_rpc_error_raises_with_code_and_data(monkeypatch):
    install_proc(monkeypatch, FakeProc(server_responder(tools_error=True)))

    async def go():
        c = MCPClient("e", {"command": "x"})
        await c.connect()
        try:
            await c.call_tool("any")
        finally:
            await c.close()
    with pytest.raises(MCPError) as ei:
        run(go())
    assert str(ei.value) == "kaput" and ei.value.code == -32000 and ei.value.data == {"x": 1}


def test_stdio_request_timeout_clears_pending(monkeypatch):
    install_proc(monkeypatch, FakeProc(lambda m: []))

    async def go():
        t = StdioTransport("silent")
        await t.connect()
        try:
            with pytest.raises(MCPError, match="timeout waiting for tools/list"):
                await t.request("tools/list", {}, timeout=0.05)
            assert t._pending == {}
        finally:
            await t.close()
    run(go())


def test_stdio_process_exit_fails_pending_with_stderr_tail(monkeypatch):
    proc = FakeProc(lambda m: [])
    install_proc(monkeypatch, proc)

    async def go():
        t = StdioTransport("dies")
        await t.connect()
        for i in range(30):
            proc.stderr.feed_data(f"line {i}\n\n".encode())
        await asyncio.sleep(0.01)
        assert len(t.stderr_tail) == 20 and t.stderr_tail[-1] == "line 29" and t.stderr_tail[0] == "line 10"
        task = asyncio.create_task(t.request("tools/list", None, timeout=5))
        await asyncio.sleep(0.01)
        proc.stdout.feed_eof()
        try:
            with pytest.raises(MCPError, match="MCP server process exited: line 29"):
                await task
        finally:
            await t.close()
    run(go())


def test_stdio_send_when_not_running():
    t = StdioTransport("x")
    with pytest.raises(MCPError, match="not running"):
        run(t.notify("x"))

    async def dead():
        t.proc = FakeProc(lambda m: []).streams()
        t.proc.returncode = 1
        await t.request("ping", None, 1)
    with pytest.raises(MCPError, match="not running"):
        run(dead())
    assert t._pending == {}


@pytest.mark.parametrize("mode,term,kill", [("ok", False, False), ("needs_term", True, False),
                                            ("needs_kill", True, True), ("term_gone", True, True)])
def test_stdio_close_escalation(monkeypatch, mode, term, kill):
    proc = FakeProc(lambda m: [], wait_mode=mode)
    install_proc(monkeypatch, proc)

    async def go():
        t = StdioTransport("x")
        await t.connect()
        await t.close()
        assert t._reader.done() and t._stderr_task.done()
    run(go())
    assert proc.stdin.closed and proc.terminated is term and proc.killed is kill


def test_stdio_close_without_process_is_noop():
    run(StdioTransport("x").close())


# ============================================================== HTTP transport
def http_transport(handler, headers=None):
    t = HTTPTransport("https://mcp.invalid/mcp", headers or {"Authorization": "Bearer k", "X-N": 5})
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


def rpc(rid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": rid}
    m.update({"error": error} if error is not None else {"result": result if result is not None else {}})
    return m


def test_http_connect_builds_client(monkeypatch):
    real = httpx.AsyncClient
    made = {}

    def fake(**kw):
        made.update(kw)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rpc(1, {"ok": 1}))))
    monkeypatch.setattr(mc.httpx, "AsyncClient", fake)

    async def go():
        t = HTTPTransport("https://mcp.invalid/mcp")
        await t.connect()
        try:
            return await t.request("x", None, 5)
        finally:
            await t.close()
    assert run(go()) == {"ok": 1}
    assert made["follow_redirects"] is True and made["timeout"].connect == 15


def test_http_json_session_and_headers():
    seen = []

    def handler(r):
        seen.append(r)
        body = json.loads(r.content)
        if body.get("id") is None:
            return httpx.Response(202)
        if r.method == "DELETE":
            return httpx.Response(405)
        return httpx.Response(200, json=rpc(body["id"], {"m": body["method"]}), headers={"Mcp-Session-Id": "S1"})

    async def go():
        c = MCPClient("web", {"url": "https://mcp.invalid/mcp", "headers": {"Authorization": "Bearer k"}})
        c.transport = http_transport(handler)
        await c.initialize()
        r = await c.transport.request("tools/list", {"cursor": "a"}, 5)
        await c.close()
        await c.close()  # second close is a no-op
        return c, r
    c, r = run(go())
    assert c.kind == "http" and r == {"m": "tools/list"}
    assert c.transport.session_id == "S1" and c.transport.protocol_version == mc.PROTOCOL_VERSION
    later = seen[-2]
    assert later.headers["mcp-session-id"] == "S1" and later.headers["mcp-protocol-version"] == mc.PROTOCOL_VERSION
    assert later.headers["authorization"] == "Bearer k" and "text/event-stream" in later.headers["accept"]
    assert seen[-1].method == "DELETE" and "mcp-session-id" in seen[-1].headers
    assert seen[0].headers.get("mcp-session-id") is None


def test_http_close_delete_failure_suppressed():
    def handler(r):
        if r.method == "DELETE":
            raise httpx.ConnectError("gone", request=r)
        return httpx.Response(200, json=rpc(1), headers={"mcp-session-id": "S"})

    async def go():
        t = http_transport(handler)
        await t.request("x", None, 5)
        await t.close()
        return t
    assert run(go())._client is None


def test_http_sse_skips_other_events_and_matches():
    sse = ("event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n"
           ": comment\n"
           "data: not-json\n\n"
           f"data: {json.dumps(rpc(99))}\n\n"
           "data: {\"jsonrpc\":\"2.0\",\n"
           "data: \"id\":1,\"result\":{\"multi\":true}}\n\n")
    t = http_transport(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream; charset=utf-8"}))
    assert run(t.request("x", None, 5)) == {"multi": True}


def test_http_sse_trailing_event_without_blank_line():
    sse = f"data: {json.dumps(rpc(1, {'tail': 1}))}"
    t = http_transport(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    assert run(t.request("x", None, 5)) == {"tail": 1}


@pytest.mark.parametrize("sse", ["", "data: {\"id\": 7, \"result\": {}}\n\n", "data: junk"])
def test_http_sse_ends_without_response(sse):
    t = http_transport(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    with pytest.raises(MCPError, match="event stream ended without a response"):
        run(t.request("x", None, 5))


@pytest.mark.parametrize("body,msg", [(b"", "empty response body"), (b"   \n", "empty response body"),
                                      (b"<html>oops</html>", "no matching JSON-RPC response"),
                                      (json.dumps(rpc(2)).encode(), "no matching JSON-RPC response"),
                                      (json.dumps({"id": 1}).encode(), "no matching JSON-RPC response")])
def test_http_junk_bodies(body, msg):
    t = http_transport(lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(MCPError, match=msg):
        run(t.request("x", None, 5))


def test_http_batch_body_and_rpc_error():
    body = json.dumps([rpc(5), rpc(1, error="plain string error")])
    t = http_transport(lambda r: httpx.Response(200, text=body))
    with pytest.raises(MCPError, match="plain string error") as ei:
        run(t.request("x", None, 5))
    assert ei.value.code is None


def test_http_status_errors_are_capped():
    t = http_transport(lambda r: httpx.Response(500, text="E" * 5000))
    with pytest.raises(MCPError) as ei:
        run(t.request("x", None, 5))
    assert ei.value.code == 500 and str(ei.value) == "HTTP 500: " + "E" * 300


def test_http_404_session_expired_vs_initialize():
    def handler(r):
        return httpx.Response(404, text="nope", headers={"mcp-session-id": "OLD"})
    t = http_transport(handler)
    with pytest.raises(MCPError, match="session expired") as ei:
        run(t.request("tools/list", None, 5))  # header sets session, so this counts as expired
    assert ei.value.code == 404
    t2 = http_transport(lambda r: httpx.Response(404, text="no such endpoint"))
    with pytest.raises(MCPError, match="HTTP 404: no such endpoint"):
        run(t2.request("tools/list", None, 5))
    t3 = http_transport(handler)
    t3.session_id = "OLD"
    with pytest.raises(MCPError, match="HTTP 404"):
        run(t3.request("initialize", {}, 5))


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_http_transport_errors_wrapped(exc):
    def handler(r):
        raise exc("boom", request=r)
    t = http_transport(handler)
    with pytest.raises(MCPError, match=f"{exc.__name__}: boom"):
        run(t.request("x", None, 5))


def test_http_notify_swallows_errors(caplog):
    calls = []

    def handler(r):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=r)
        if len(calls) == 2:
            return httpx.Response(500, text="err")
        return httpx.Response(202)
    t = http_transport(handler)
    with caplog.at_level(logging.DEBUG, logger="avrone.mcp"):
        run(t.notify("notifications/initialized"))
        run(t.notify("notifications/x", {"a": 1}))
        assert run(t.notify("notifications/y")) is None
    assert len(calls) == 3 and "notify notifications/initialized failed" in caplog.text


def test_http_post_requires_connect():
    with pytest.raises(AssertionError):
        run(HTTPTransport("https://x.invalid")._post({}, 1, 1))
    run(HTTPTransport("https://x.invalid").close())  # never connected: no-op


# ============================================================== helpers
@pytest.mark.parametrize("payload,want", [
    ("junk", None), ("[]", None), ("null", None), ('"s"', None), ('[1, "a", null]', None),
    ('{"id": 1}', None), ('{"id": "1", "result": 1}', None),
    ('{"id": 1, "error": {"message": "m"}}', {"id": 1, "error": {"message": "m"}}),
    ('[{"id": 2, "result": 0}, {"id": 1, "result": 0}]', {"id": 1, "result": 0}),
])
def test_match_response(payload, want):
    assert _match_response(payload, 1) == want


def test_unwrap_variants():
    assert _unwrap({"result": 3}) == 3
    assert _unwrap({"error": None, "result": 4}) == 4
    assert _unwrap({}) is None
    with pytest.raises(MCPError, match="error"):
        _unwrap({"error": {}})
    with pytest.raises(MCPError, match="\\['x'\\]"):
        _unwrap({"error": ["x"]})


def test_format_content_all_kinds_and_caps():
    big = "A" * 1_000_000
    res = {"content": ["raw", {"type": "text"}, {"type": "image", "data": big, "mimeType": "image/png"},
                       {"type": "image"}, {"type": "audio"},
                       {"type": "resource", "resource": {"uri": "file:///a", "text": "body"}},
                       {"type": "resource", "resource": {"uri": "file:///b"}}, {"type": "resource"},
                       {"type": "resource_link", "uri": "u://x", "name": "nm"}, {"type": "resource_link"},
                       {"type": "video"}, {}]}
    out = format_content(res).splitlines()
    assert out[0] == "raw" and out[1] == ""
    assert out[2] == "[image: image/png, 1000000 b64 chars omitted]" and "A" * 100 not in "\n".join(out)
    assert out[3] == "[image: unknown, 0 b64 chars omitted]" and out[4] == "[audio: unknown omitted]"
    assert out[5:7] == ["[resource file:///a]", "body"]
    assert out[7] == "[resource: file:///b (binary) omitted]" and out[8] == "[resource:  (binary) omitted]"
    assert out[9] == "[resource link: u://x nm]" and out[10] == "[resource link:  ]"
    assert out[11] == "[video content omitted]" and out[12] == "[None content omitted]"


def test_format_content_structured_and_empty():
    assert format_content({"structuredContent": {"k": "é"}}) == '{"k": "é"}'
    assert format_content({"content": [], "structuredContent": None}) == "(no content)"
    assert format_content({"content": None}) == "(no content)"
    assert format_content({"content": [{"type": "text", "text": "x"}], "structuredContent": {"a": 1}}) == "x"


def test_args_doc_edges():
    assert args_doc_from_schema(None) == "{}" and args_doc_from_schema("x") == "{}"  # type: ignore[arg-type]
    assert args_doc_from_schema({}) == "{}"
    doc = args_doc_from_schema({"properties": {
        "a": {"type": ["string", "null"]}, "b": {"type": ["null"]}, "c": {"enum": list(range(10))},
        "d": {}, "e": "notadict", "f": {"type": "weird"}, "g": {"type": ["integer", "custom"]}},
        "required": ["a"]})
    assert doc == '{"a": str, "b"?: any, "c"?: 0|1|2|3|4|5, "d"?: any, "e"?: any, "f"?: any, "g"?: int|custom}'


def test_args_doc_truncated_to_limit():
    schema = {"properties": {f"field_{i}": {"type": "string"} for i in range(100)}}
    doc = args_doc_from_schema(schema, limit=80)
    assert len(doc) <= 80 and doc.endswith(", ...}") and doc.startswith('{"field_0"?: str')


def test_load_config_variants(tmp_path, monkeypatch):
    assert load_config(tmp_path / "missing.json") == {}
    p = tmp_path / "mcp.json"
    p.write_text("")
    assert load_config(p) == {}
    p.write_text("[1, 2]")
    assert load_config(p) == {}
    p.write_text(json.dumps({"servers": {"a": {"command": "x"}, "b": {"url": "u", "disabled": True}, "c": "junk",
                                         1: {"command": "y"}}}))
    assert load_config(p) == {"a": {"command": "x"}, "1": {"command": "y"}}
    monkeypatch.setenv("AVRONE_MCP_CONFIG", str(p))
    assert set(load_config()) == {"a", "1"}
    p.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        load_config(p)


def test_client_requires_command_or_url():
    with pytest.raises(MCPError, match="needs either 'command' or 'url'"):
        MCPClient("x", {})
    assert MCPClient("y", {"url": "https://a.invalid", "command": "ignored"}).kind == "http"


def test_initialize_unknown_version_and_empty_result(caplog):
    class T(HTTPTransport):
        def __init__(self, result):
            super().__init__("https://x.invalid")
            self.result, self.notes = result, []

        async def request(self, method, params, timeout):
            return self.result

        async def notify(self, method, params=None):
            self.notes.append(method)
    c = MCPClient("s", {"url": "https://x.invalid"})
    c.transport = T({"protocolVersion": "1999-01-01"})
    with caplog.at_level(logging.WARNING, logger="avrone.mcp"):
        run(c.initialize())
    assert c.protocol_version == "1999-01-01" == c.transport.protocol_version and "unknown protocol" in caplog.text
    c.transport = T(None)
    assert run(c.initialize()) == {} and c.protocol_version == mc.PROTOCOL_VERSION
    assert c.server_info == {} and c.capabilities == {} and c.transport.notes == ["notifications/initialized"]


def test_list_tools_pagination_guard_and_empty_call():
    class Loop(mc._Transport):
        n = 0

        async def request(self, method, params, timeout):
            self.n += 1
            if method == "tools/call":
                return None
            return {"tools": [{"name": "same"}], "nextCursor": "again"}
    c = MCPClient("s", {"command": "x"})
    c.transport = Loop()
    tools = run(c.list_tools())
    assert c.transport.n == 100 and len(tools) == 100
    assert run(c.call_tool("t")) == "(no content)"


# ============================================================== manager
class FakeClient:
    """Stands in for MCPClient inside MCPManager."""
    behaviour: dict = {}

    def __init__(self, name, cfg, timeout=30):
        b = self.behaviour.get(name, {})
        if b.get("init_error"):
            raise MCPError("bad config")
        self.name, self.kind, self.timeout = name, "stdio", 1.0
        self.server_info, self.protocol_version = {"name": name}, "2025-06-18"
        self.b = b
        self.closed = False

    async def connect(self):
        if self.b.get("connect_error"):
            raise self.b["connect_error"]

    async def list_tools(self):
        return self.b.get("tools", [])

    async def call_tool(self, tool, args):
        if self.b.get("call_error"):
            raise MCPError("tool broke")
        return f"{tool}:{json.dumps(args)}"

    async def close(self):
        self.closed = True
        if self.b.get("close_error"):
            raise RuntimeError("close failed")


def test_manager_config_error(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text("{broken")
    m = MCPManager(config_path=p)
    st = run(m.start(ToolRegistry()))
    assert st["_config"]["connected"] is False and st["_config"]["error"].startswith("config:")


def test_manager_mixed_servers(monkeypatch):
    FakeClient.behaviour = {
        "noconf": {"init_error": True},
        "down": {"connect_error": ConnectionRefusedError("refused"), "close_error": True},
        "empty": {"connect_error": asyncio.TimeoutError()},
        "ok": {"tools": [{"name": "read file", "description": "Reads\na file", "inputSchema": {"properties": {"p": {}}}},
                         {"name": "dup", "title": "T"}, {"name": "dup"}, {"name": "x" * 10}]},
        "brk": {"tools": [{"name": "t"}], "call_error": True},
    }
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    class PickyRegistry(ToolRegistry):
        def register(self, tool):
            if tool.name.endswith("xxxxxxxxxx"):
                raise ValueError("rejected")
            super().register(tool)
    reg = PickyRegistry()
    reg.register(Tool("keep_me", "pre-existing", "{}", lambda c, a: "x"))
    m = MCPManager(servers={n: {"command": "c"} for n in FakeClient.behaviour})

    async def go():
        st = await m.start(reg)
        fn = reg.get("mcp__ok__read_file").fn
        out_ok = await fn(None, {"a": 1})
        out_junk = await fn(None, ["not", "a", "dict"])
        out_err = await reg.get("mcp__brk__t").fn(None, {})
        await m.stop()
        return st, out_ok, out_junk, out_err
    st, out_ok, out_junk, out_err = run(go())
    assert st["noconf"] == {"connected": False, "error": "MCPError: bad config", "tools": 0, "transport": None}
    assert st["down"]["error"] == "ConnectionRefusedError: refused" and st["down"]["transport"] == "stdio"
    assert st["empty"]["error"] == "TimeoutError: " and st["empty"]["connected"] is False
    assert st["ok"]["connected"] and st["ok"]["tools"] == 3 and st["ok"]["protocol"] == "2025-06-18"
    assert out_ok == 'read file:{"a": 1}' and out_junk == "read file:{}"
    assert out_err == "ERROR: MCP brk/t: tool broke"
    after = m.status()
    assert after["ok"]["connected"] is False and after["brk"]["connected"] is False
    assert "mcp__ok__read_file" not in reg.names() and "mcp__ok__dup" not in reg.names()
    assert reg.names() == ["keep_me"]
    assert m.clients == {} and m._registered == []


def test_manager_description_fallbacks_and_caps(monkeypatch):
    FakeClient.behaviour = {"s": {"tools": [{"name": "a"}, {"name": "b", "description": "D" * 900}]}}
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    reg = ToolRegistry()
    run(MCPManager(servers={"s": {"command": "c"}}).start(reg))
    assert reg.get("mcp__s__a").description == "MCP tool a on s"
    assert reg.get("mcp__s__b").description == "D" * 500
    assert reg.get("mcp__s__a").source == "mcp:s"


def test_manager_stop_before_start():
    m = MCPManager(servers={})
    run(m.stop())
    assert m.status() == {}


def test_safe_part():
    assert mc._safe_part("a b/c:d") == "a_b_c_d" and mc._safe_part("") == "_" and mc._safe_part("ok-1.2") == "ok-1.2"
