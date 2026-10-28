import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from avrone.mcp_client import MCPManager, args_doc_from_schema, format_content
from avrone.plugins import load_plugins
from avrone.tools import ToolRegistry, builtin_registry

ROOT = Path(__file__).resolve().parent.parent

FAKE_SERVER = textwrap.dedent(r'''
    import json, sys
    TOOLS = [
        {"name": "echo", "description": "echo text back",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
                         "required": ["text"]}},
        {"name": "boom", "description": "always fails", "inputSchema": {"type": "object", "properties": {}}},
    ]
    def send(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        m = json.loads(line)
        if "id" not in m:
            continue  # notification
        mid, meth, p = m["id"], m["method"], m.get("params") or {}
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
        elif meth == "tools/list":
            # paginate: one tool per page
            if p.get("cursor") == "page2":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[1:]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:1], "nextCursor": "page2"}})
        elif meth == "tools/call":
            a = p.get("arguments") or {}
            if p["name"] == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": a["text"] * int(a.get("times", 1))},
                    {"type": "image", "data": "AAAA", "mimeType": "image/png"}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                      "content": [{"type": "text", "text": "kaboom"}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
''')


def test_mcp_manager_registers_and_calls_stdio_tool(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(script)]},
        "broken": {"command": str(tmp_path / "does-not-exist")},
    }}))

    async def go():
        reg = builtin_registry()
        mgr = MCPManager(cfg)
        status = await mgr.start(reg)
        try:
            assert status["fake"]["connected"] is True and status["fake"]["tools"] == 2
            assert status["broken"]["connected"] is False and status["broken"]["error"]
            tool = reg.get("mcp__fake__echo")
            assert tool is not None and tool.source == "mcp:fake"
            assert tool.args_doc == '{"text": str, "times"?: int}'
            out = await reg.call(None, "mcp__fake__echo", {"text": "hi", "times": 3})
            assert out.startswith("hihihi") and "[image: image/png" in out
            err = await reg.call(None, "mcp__fake__boom", {})
            assert err.startswith("ERROR") and "kaboom" in err
            assert "mcp__fake__echo" in reg.docs()
        finally:
            await mgr.stop()
        assert reg.get("mcp__fake__echo") is None
        assert mgr.status()["fake"]["connected"] is False

    asyncio.run(go())


def test_mcp_manager_missing_config_is_harmless(tmp_path):
    async def go():
        mgr = MCPManager(tmp_path / "nope.json")
        assert await mgr.start(ToolRegistry()) == {}
        await mgr.stop()
    asyncio.run(go())


def test_helpers():
    assert args_doc_from_schema({"type": "object", "properties": {"m": {"enum": ["a", "b"]}}}) == '{"m"?: "a"|"b"}'
    assert format_content({"content": [{"type": "resource", "resource": {"uri": "file://x", "text": "body"}}]}) \
        == "[resource file://x]\nbody"


def test_plugins_load_examples_and_calculator_works():
    reg = ToolRegistry()
    reports = load_plugins(reg, None, directory=ROOT / "plugins", include_entry_points=False)
    by_name = {r["name"]: r for r in reports}
    assert by_name["calculator"]["error"] is None and by_name["calculator"]["tools"] == ["calculator"]
    assert by_name["datetime_tool"]["error"] is None and by_name["datetime_tool"]["tools"] == ["current_time"]
    assert reg.get("calculator").source == "plugin:calculator"

    async def go():
        assert await reg.call(None, "calculator", {"expression": "(2+3)*4 - 2^3"}) == "(2+3)*4 - 2^3 = 12"
        assert (await reg.call(None, "calculator", {"expression": "sqrt(16) + pi*0"})).endswith("= 4")
        assert (await reg.call(None, "calculator", {"expression": "__import__('os')"})).startswith("ERROR")
        assert (await reg.call(None, "calculator", {"expression": "1/0"})) == "ERROR: division by zero"
        assert "Asia/Tokyo" in await reg.call(None, "current_time", {"timezone": "Asia/Tokyo"})
        assert (await reg.call(None, "current_time", {"timezone": "Mars/Base"})).startswith("ERROR")
    asyncio.run(go())


def test_bad_plugin_is_reported_not_fatal(tmp_path):
    (tmp_path / "bad.py").write_text("raise RuntimeError('nope')\n")
    (tmp_path / "noreg.py").write_text("x = 1\n")
    reports = load_plugins(ToolRegistry(), None, directory=tmp_path, include_entry_points=False)
    assert {r["name"] for r in reports} == {"bad", "noreg"}
    assert all(r["error"] for r in reports)


def test_avrone_mcp_server_over_stdio(tmp_path):
    env = {**os.environ, "AVRONE_LLM_MODE": "mock", "AVRONE_RUNNER": "local",
           "AVRONE_WORKSPACE": str(tmp_path / "ws"), "AVRONE_DATA_DIR": str(tmp_path / "data"),
           "AVRONE_MCP_CONFIG": str(tmp_path / "none.json"), "AVRONE_PLUGINS_DIR": str(ROOT / "plugins"),
           "AVRONE_ENV_FILE": str(tmp_path / "none.env"), "PYTHONPATH": str(ROOT)}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "avrone_tools", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "avrone_chat", "arguments": {"task": "hi"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "bogus"},
    ]
    proc = subprocess.run([sys.executable, "-m", "avrone.mcp_server"], cwd=tmp_path, env=env,
                          input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True,
                          text=True, timeout=60)
    replies = {}
    for line in proc.stdout.splitlines():
        obj = json.loads(line)  # stdout must be pure JSON-RPC
        replies[obj["id"]] = obj
    assert set(replies) == {1, 2, 3, 4, 5, 6}, proc.stderr
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["result"]["serverInfo"]["name"] == "avrone"
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert names == {"avrone_squad", "avrone_chat", "avrone_deep", "avrone_tools"}
    assert replies[3]["result"] == {}
    listed = replies[4]["result"]["content"][0]["text"]
    assert "shell" in listed and "calculator" in listed
    assert replies[5]["result"]["isError"] is False
    assert "Mock worker result" in replies[5]["result"]["content"][0]["text"]
    assert replies[6]["error"]["code"] == -32601
