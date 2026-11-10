"""Round 3 MCP edges: JSON-RPC protocol quirks, framing mismatches, concurrency, EPIPE."""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from avrone import mcp_server as ms

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 25


# --------------------------------------------------------------------------- helpers (stdio child)
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


def run_mcp_bytes(tmp_path: Path, payload: bytes, timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "-m", "avrone.mcp_server"],
            cwd=tmp_path,
            env=_env(tmp_path),
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"mcp_server hung after {timeout}s\nstdout={e.stdout!r}\nstderr={e.stderr!r}")


def run_mcp_chunked(tmp_path: Path, chunks: list[bytes], timeout: float = TIMEOUT,
                    pause: float = 0.04) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        [sys.executable, "-m", "avrone.mcp_server"],
        cwd=tmp_path,
        env=_env(tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        for chunk in chunks:
            proc.stdin.write(chunk)
            proc.stdin.flush()
            time.sleep(pause)
        proc.stdin.close()
        proc.stdin = None
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"mcp_server hung after {timeout}s (chunked)\nstdout={out!r}\nstderr={err!r}")


def parse_rpc(proc: subprocess.CompletedProcess) -> list[dict]:
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    lines = [ln for ln in proc.stdout.split(b"\n") if ln]
    return [json.loads(ln) for ln in lines]


# --------------------------------------------------------------------------- helpers (in-process)
class FakeTools:
    def list(self):
        return [{"name": "shell", "source": "builtin", "args": "{}", "description": "run"}]


class FakeCore:
    def __init__(self):
        self.tools = FakeTools()
        self.calls = []

    async def squad(self, task, size=None):
        self.calls.append(("squad", task, size))
        return {"answer": f"squad:{task}", "run_id": "r1", "agents": [1, 2]}

    async def chat(self, task):
        self.calls.append(("chat", task))
        return "ok"

    async def deep(self, task, first=None):
        self.calls.append(("deep", task, first))
        return {"answer": "deep", "run_id": "r2"}


class SlowCore(FakeCore):
    async def chat(self, task):
        await asyncio.sleep(0.25)
        self.calls.append(("chat", task))
        return f"slow:{task}"


class NanCore(FakeCore):
    async def chat(self, task):
        return float("nan")


def _server(core):
    async def factory():
        return core, {}
    return ms.AvroneMCPServer(core_factory=factory)


def _serve_lines(lines, core=None):
    async def go():
        s = _server(core or FakeCore())
        reader = asyncio.StreamReader()
        for ln in lines:
            if isinstance(ln, bytes):
                raw = ln if ln.endswith(b"\n") else ln + b"\n"
            else:
                raw = ((ln if isinstance(ln, str) else json.dumps(ln)).encode() + b"\n")
            reader.feed_data(raw)
        reader.feed_eof()
        out = io.BytesIO()
        await s.serve(reader, out)
        return [json.loads(x) for x in out.getvalue().decode().splitlines() if x]
    return asyncio.run(go())


# --------------------------------------------------------------------------- JSON-RPC protocol
def test_batch_mixes_requests_and_notifications():
    """A batch of pings plus a notification must reply only to the requests, in any order."""
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "two", "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"id": 1}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    msgs = _serve_lines([batch])
    ids = {m.get("id") for m in msgs}
    assert ids == {1, "two", 3}
    assert all("result" in m for m in msgs)
    assert len(msgs) == 3


def test_id_null_still_gets_a_response():
    msgs = _serve_lines([{"jsonrpc": "2.0", "id": None, "method": "ping"}])
    assert len(msgs) == 1
    assert msgs[0]["id"] is None
    assert "result" in msgs[0] or "error" in msgs[0]


def test_id_float_is_echoed_not_a_crash():
    msgs = _serve_lines([{"jsonrpc": "2.0", "id": 1.5, "method": "ping"}])
    assert len(msgs) == 1
    assert msgs[0]["id"] == pytest.approx(1.5)
    assert "result" in msgs[0]


def test_id_bool_does_not_crash_the_stream():
    # JSON-RPC forbids boolean ids; the server may error or echo, but must reply once and stay up.
    msgs = _serve_lines([
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ])
    assert any(m.get("id") == 2 and "result" in m for m in msgs)
    assert len(msgs) >= 1


def test_params_as_array_is_invalid_params():
    msgs = _serve_lines([{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2, 3]}])
    assert len(msgs) == 1
    assert msgs[0]["error"]["code"] == -32602


def test_unknown_params_on_ping_are_ignored():
    msgs = _serve_lines([{"jsonrpc": "2.0", "id": 7, "method": "ping",
                         "params": {"unexpected": True, "extra": [1, 2]}}])
    assert msgs == [{"jsonrpc": "2.0", "id": 7, "result": {}}]


def test_empty_and_unknown_method_names():
    msgs = _serve_lines([
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        {"jsonrpc": "2.0", "id": 2, "method": "PING"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call "},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ])
    by = {m["id"]: m for m in msgs}
    assert by[1]["error"]["code"] == -32601
    assert by[2]["error"]["code"] == -32601
    assert by[3]["error"]["code"] == -32601
    assert "result" in by[4]


def test_tools_call_arguments_as_array_is_invalid():
    msgs = _serve_lines([{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "avrone_chat", "arguments": ["not", "an", "object"]},
    }])
    assert len(msgs) == 1
    assert "error" in msgs[0]


# --------------------------------------------------------------------------- framing / encoding
def test_content_length_header_then_valid_newline_frame(tmp_path):
    """A stray Content-Length header is a parse error; the following newline-framed ping still works."""
    ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    payload = f"Content-Length: {len(ping)}\n{ping}\n".encode()
    msgs = parse_rpc(run_mcp_bytes(tmp_path, payload))
    assert any(m.get("id") == 9 and "result" in m for m in msgs)
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)


def test_truncated_multibyte_utf8_across_chunks(tmp_path):
    msg = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping",
                      "params": {"note": "before🌍after"}}, ensure_ascii=False)
    raw = (msg + "\n").encode("utf-8")
    idx = raw.index("🌍".encode("utf-8"))
    proc = run_mcp_chunked(tmp_path, [raw[: idx + 2], raw[idx + 2 :]])
    msgs = parse_rpc(proc)
    assert any(m.get("id") == 4 and "result" in m for m in msgs)


def test_embedded_nul_then_recover(tmp_path):
    bad = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\x00\n'
    good = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n"
    msgs = parse_rpc(run_mcp_bytes(tmp_path, bad + good))
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)
    assert any(m.get("id") == 2 and "result" in m for m in msgs)


def test_trailing_garbage_after_valid_json_then_recover(tmp_path):
    garbage = b'{"jsonrpc":"2.0","id":1,"method":"ping"} trailing-junk\n'
    good = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n"
    msgs = parse_rpc(run_mcp_bytes(tmp_path, garbage + good))
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)
    assert any(m.get("id") == 2 and "result" in m for m in msgs)


def test_empty_and_whitespace_only_lines_are_skipped(tmp_path):
    payload = b"\n   \n\t\n" + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}).encode() + b"\n"
    msgs = parse_rpc(run_mcp_bytes(tmp_path, payload))
    assert msgs and all(m.get("id") == 3 and "result" in m for m in msgs)


@pytest.mark.timeout(45)
def test_just_under_16mib_line_is_accepted(tmp_path):
    """A single line just under the StreamReader 16 MiB cap must still parse and ping."""
    limit = 16 * 1024 * 1024
    prefix = '{"jsonrpc":"2.0","id":11,"method":"ping","params":{"pad":"'
    suffix = '"}}\n'
    pad = limit - len(prefix) - len(suffix) - 64  # leave a comfortable margin under the cap
    assert pad > 1_000_000
    payload = prefix.encode() + (b"x" * pad) + suffix.encode()
    assert len(payload) < limit
    msgs = parse_rpc(run_mcp_bytes(tmp_path, payload, timeout=45))
    assert any(m.get("id") == 11 and "result" in m for m in msgs)


def test_interleaved_partial_frames_from_two_writers_then_recover(tmp_path):
    """Two logical writers interleave incomplete objects; the stream must recover on a later ping."""
    chunks = [
        b'{"jsonrpc":"2.0","id":1,',
        b'{"jsonrpc":"2.0","id":2,',
        b'"method":"ping"}\n',
        b'"method":"ping"}\n',
        json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}).encode() + b"\n",
    ]
    msgs = parse_rpc(run_mcp_chunked(tmp_path, chunks, pause=0.03))
    assert any(m.get("id") == 99 and "result" in m for m in msgs)
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)


# --------------------------------------------------------------------------- concurrency / shutdown
def test_shutdown_while_requests_are_in_flight():
    """EOF mid-flight must wait for in-flight tools/call tasks and exit cleanly."""
    core = SlowCore()

    async def go():
        s = _server(core)
        reader = asyncio.StreamReader()
        out = io.BytesIO()
        t = asyncio.create_task(s.serve(reader, out))
        for i in range(3):
            req = {"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                   "params": {"name": "avrone_chat", "arguments": {"task": f"t{i}"}}}
            reader.feed_data(json.dumps(req).encode() + b"\n")
        await asyncio.sleep(0.05)  # tasks started, still sleeping in chat()
        reader.feed_eof()
        await asyncio.wait_for(t, timeout=5)
        return [json.loads(x) for x in out.getvalue().decode().splitlines() if x]

    msgs = asyncio.run(go())
    ids = {m.get("id") for m in msgs}
    assert ids == {1, 2, 3}
    assert all("result" in m or "error" in m for m in msgs)


def test_simultaneous_tools_call_mixed_valid_and_invalid_ids():
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "avrone_chat", "arguments": {"task": "ok"}}},
        {"jsonrpc": "2.0", "id": "bad-name", "method": "tools/call",
         "params": {"name": "does_not_exist", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "avrone_chat"}},  # missing arguments: should still run with {}
        {"jsonrpc": "2.0", "id": None, "method": "tools/call",
         "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
    ]
    msgs = _serve_lines(reqs)
    by_id = {m["id"]: m for m in msgs}
    assert "result" in by_id[1]
    assert by_id["bad-name"]["error"]["code"] == -32602
    assert "result" in by_id[5]
    assert None in by_id  # id=null still answered


def test_epipe_on_send_does_not_kill_serve():
    class Boom(io.BytesIO):
        def write(self, b):
            raise BrokenPipeError("epipe")

        def flush(self):
            return None

    async def go():
        s = _server(FakeCore())
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n")
        reader.feed_eof()
        await asyncio.wait_for(s.serve(reader, Boom()), timeout=5)

    asyncio.run(go())  # must not raise


def test_non_finite_tool_result_becomes_internal_error_not_a_crash():
    msgs = _serve_lines([{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "avrone_chat", "arguments": {"task": "x"}},
    }], core=NanCore())
    assert len(msgs) == 1
    # Either the tool layer stringified NaN, or send() fell back to -32603. Either is a non-crash.
    body = msgs[0]
    if "error" in body:
        assert body["error"]["code"] == -32603
    else:
        # If it was wrapped as text, the encoded stream must still be strict JSON (already parsed).
        assert "result" in body
        json.dumps(body, allow_nan=False)
