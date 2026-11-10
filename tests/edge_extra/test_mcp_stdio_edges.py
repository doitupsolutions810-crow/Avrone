"""Extra stdio-server edge cases: never hang, always emit valid JSON-RPC."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 20


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


def run_mcp(tmp_path: Path, payload: str, timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "-m", "avrone.mcp_server"],
            cwd=tmp_path,
            env=_env(tmp_path),
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        pytest.fail(
            f"mcp_server hung after {timeout}s\n"
            f"stdout={e.stdout!r}\nstderr={e.stderr!r}"
        )


def parse_rpc(proc: subprocess.CompletedProcess) -> list[dict]:
    msgs = []
    for i, line in enumerate(proc.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout line {i} is not valid JSON-RPC: {line!r} ({e})\nstderr={proc.stderr}")
        assert isinstance(obj, dict), f"JSON-RPC message must be an object: {obj!r}"
        assert obj.get("jsonrpc") == "2.0", obj
        assert "result" in obj or "error" in obj, obj
        msgs.append(obj)
    return msgs


def by_id(msgs: list[dict]) -> dict:
    return {m.get("id"): m for m in msgs if "id" in m}


def test_immediate_eof_exits_promptly(tmp_path):
    proc = run_mcp(tmp_path, "", timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert parse_rpc(proc) == []


def test_malformed_json_line_returns_parse_error(tmp_path):
    payload = (
        "this is not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
    )
    proc = run_mcp(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)
    assert by_id(msgs)[1]["result"] == {}


def test_batch_array_of_requests(tmp_path):
    batch = [
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        {"jsonrpc": "2.0", "id": 11, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 12, "method": "does-not-exist"},
    ]
    proc = run_mcp(tmp_path, json.dumps(batch) + "\n")
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert set(replies) >= {10, 11, 12}
    assert replies[10]["result"] == {}
    assert "protocolVersion" in replies[11]["result"]
    assert replies[12]["error"]["code"] == -32601


def test_empty_batch_is_invalid_request(tmp_path):
    proc = run_mcp(tmp_path, "[]\n")
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert msgs and msgs[0]["error"]["code"] == -32600


def test_unknown_method(tmp_path):
    proc = run_mcp(tmp_path, json.dumps({"jsonrpc": "2.0", "id": 7, "method": "totally_bogus"}) + "\n")
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[7]["error"]["code"] == -32601


def test_tools_call_bad_arguments(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "avrone_chat", "arguments": "not-an-object"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "avrone_chat", "arguments": {"task": 123}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "avrone_chat", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": "not-an-object"},
    ]
    proc = run_mcp(tmp_path, "".join(json.dumps(m) + "\n" for m in msgs))
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert set(replies) >= {1, 2, 3, 4, 5, 6}
    assert replies[2]["error"]["code"] == -32602
    # task must be a string: tool-level error payload, still valid JSON-RPC result
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["error"]["code"] == -32602
    assert replies[5]["result"]["isError"] is True
    assert "task" in replies[5]["result"]["content"][0]["text"]
    assert replies[6]["error"]["code"] == -32602


def test_large_valid_then_ping(tmp_path):
    # A ~1 MiB but valid request must not hang and must still leave the server serving later requests.
    huge = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (1024 * 1024)}}
    payload = json.dumps(huge) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    proc = run_mcp(tmp_path, payload, timeout=30)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[1]["result"] == {}
    assert replies[2]["result"] == {}


def test_oversized_line_does_not_kill_server(tmp_path):
    # StreamReader limit is 16 MiB; a line just over it should yield Invalid Request, then still serve ping.
    # Use a slightly-over-limit line; wrap in a slightly longer timeout for I/O.
    over = "n" * (16 * 1024 * 1024 + 64) + "\n"
    payload = over + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
    proc = run_mcp(tmp_path, payload, timeout=45)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert any(m.get("error", {}).get("code") == -32600 for m in msgs)
    assert by_id(msgs)[9]["result"] == {}
