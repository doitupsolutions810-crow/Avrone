"""Harder MCP stdio edges: chunked frames, notifications, deep JSON, bad UTF-8, batches."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 20


def _env(tmp_path: Path, extra: dict[str, str] | None = None, drop: set[str] | None = None) -> dict[str, str]:
    env = {
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
    if extra:
        env.update(extra)
    for k in drop or ():
        env.pop(k, None)
    return env


def run_mcp_bytes(tmp_path: Path, payload: bytes, timeout: float = TIMEOUT,
                  env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "-m", "avrone.mcp_server"],
            cwd=tmp_path,
            env=env or _env(tmp_path),
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        pytest.fail(
            f"mcp_server hung after {timeout}s\n"
            f"stdout={e.stdout!r}\nstderr={e.stderr!r}"
        )


def run_mcp_chunked(tmp_path: Path, chunks: list[bytes], timeout: float = TIMEOUT,
                    pause: float = 0.05) -> subprocess.CompletedProcess:
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
        proc.stdin = None  # communicate() must not flush an already-closed stdin
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"mcp_server hung after {timeout}s (chunked)\nstdout={out!r}\nstderr={err!r}")


def parse_rpc(proc: subprocess.CompletedProcess) -> list[dict]:
    stdout = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, (bytes, bytearray)) else proc.stdout
    msgs = []
    for i, line in enumerate(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout line {i} is not valid JSON-RPC: {line!r} ({e})\nstderr={proc.stderr!r}")
        assert isinstance(obj, dict), f"JSON-RPC message must be an object: {obj!r}"
        assert obj.get("jsonrpc") == "2.0", obj
        assert "result" in obj or "error" in obj, obj
        msgs.append(obj)
    return msgs


def by_id(msgs: list[dict]) -> dict:
    return {m.get("id"): m for m in msgs if "id" in m and m.get("id") is not None}


def _ping(i) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": "ping"}


def test_chunked_partial_frame_then_complete(tmp_path):
    first = b'{"jsonrpc":"2.0","id":1,"method":'
    rest = b'"ping"}\n' + (json.dumps(_ping(2)) + "\n").encode()
    proc = run_mcp_chunked(tmp_path, [first, rest], pause=0.08)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[1]["result"] == {}
    assert replies[2]["result"] == {}


def test_last_line_without_newline_still_served(tmp_path):
    payload = json.dumps(_ping(1)).encode()  # no trailing newline; EOF should flush the frame
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[1]["result"] == {}


def test_crlf_and_blank_lines(tmp_path):
    payload = (
        json.dumps(_ping(1)) + "\r\n\r\n"
        + json.dumps(_ping(2)) + "\r\n"
    ).encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[1]["result"] == {} and replies[2]["result"] == {}


def test_utf8_bom_then_ping(tmp_path):
    payload = b"\xef\xbb\xbf" + (json.dumps(_ping(1)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    # BOM may make the first line a parse error; a follow-up ping must still work.
    payload = b"\xef\xbb\xbf" + (json.dumps(_ping(1)) + "\n" + json.dumps(_ping(2)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    # At least the second request (clean JSON after the bom-tainted first line, or both) is answered.
    assert 2 in replies or 1 in replies
    if 2 in replies:
        assert replies[2]["result"] == {}


def test_invalid_utf8_bytes_then_ping(tmp_path):
    payload = b"\xff\xfe this is not utf-8\n" + (json.dumps(_ping(3)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)
    assert by_id(msgs)[3]["result"] == {}


def test_notifications_produce_no_response(tmp_path):
    payload = "".join(json.dumps(m) + "\n" for m in [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "ping"},  # notification: no id
        {"jsonrpc": "2.0", "method": "does-not-exist"},  # notification of unknown method
        _ping(9),
    ]).encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert all(m.get("id") is not None for m in msgs)
    replies = by_id(msgs)
    assert set(replies) == {9}
    assert replies[9]["result"] == {}


def test_duplicate_request_ids_both_answered(tmp_path):
    payload = "".join(json.dumps(m) + "\n" for m in [_ping(1), _ping(1), _ping(2)]).encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    ones = [m for m in msgs if m.get("id") == 1]
    assert len(ones) == 2
    assert all(m["result"] == {} for m in ones)
    assert by_id(msgs)[2]["result"] == {}


def test_interleaved_many_requests_one_payload(tmp_path):
    msgs_in = [_ping(i) for i in range(1, 21)]
    msgs_in.insert(5, {"jsonrpc": "2.0", "id": 100, "method": "nope"})
    payload = "".join(json.dumps(m) + "\n" for m in msgs_in).encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert set(range(1, 21)).issubset(replies)
    assert replies[100]["error"]["code"] == -32601
    for i in range(1, 21):
        assert replies[i]["result"] == {}


def test_oversized_but_valid_batch(tmp_path):
    batch = [_ping(i) for i in range(1, 41)]
    proc = run_mcp_bytes(tmp_path, (json.dumps(batch) + "\n").encode())
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert set(replies) == set(range(1, 41))
    assert all(replies[i]["result"] == {} for i in range(1, 41))


def test_batch_with_non_object_items(tmp_path):
    batch = [_ping(1), 42, "nope", None, _ping(2)]
    proc = run_mcp_bytes(tmp_path, (json.dumps(batch) + "\n").encode())
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    replies = by_id(msgs)
    assert replies[1]["result"] == {} and replies[2]["result"] == {}
    assert sum(1 for m in msgs if m.get("error", {}).get("code") == -32600) >= 3


def test_top_level_scalar_is_invalid_request_then_ping(tmp_path):
    payload = b"42\n" + (json.dumps(_ping(8)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    assert any(m.get("error", {}).get("code") == -32600 for m in msgs)
    assert by_id(msgs)[8]["result"] == {}


def test_deeply_nested_params_or_parse_error_then_ping(tmp_path):
    depth = 2000
    # Build a valid JSON object whose params value is deeply nested.
    nested = "{" + '"k":{' * depth + "}" * depth + "}"
    line = '{"jsonrpc":"2.0","id":1,"method":"ping","params":' + nested + "}\n"
    payload = line.encode() + (json.dumps(_ping(2)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload, timeout=30)
    assert proc.returncode == 0, proc.stderr
    msgs = parse_rpc(proc)
    replies = by_id(msgs)
    # Either the deep request is answered (ping ignores unused params) or it is a parse/invalid error.
    if 1 in replies:
        assert "result" in replies[1] or "error" in replies[1]
    else:
        assert any(m.get("error", {}).get("code") in (-32700, -32600) for m in msgs)
    assert replies[2]["result"] == {}


def test_method_must_be_a_string(tmp_path):
    payload = (json.dumps({"jsonrpc": "2.0", "id": 4, "method": 123}) + "\n"
               + json.dumps(_ping(5)) + "\n").encode()
    proc = run_mcp_bytes(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(parse_rpc(proc))
    assert replies[4]["error"]["code"] == -32600
    assert replies[5]["result"] == {}


def test_missing_home_env_still_serves(tmp_path):
    env = _env(tmp_path, drop={"HOME"})
    proc = run_mcp_bytes(tmp_path, (json.dumps(_ping(1)) + "\n").encode(), env=env)
    assert proc.returncode == 0, proc.stderr
    assert by_id(parse_rpc(proc))[1]["result"] == {}


def test_rapid_connect_shutdown_cycles(tmp_path):
    for i in range(8):
        cycle = tmp_path / f"c{i}"
        cycle.mkdir()
        proc = run_mcp_bytes(cycle, (json.dumps(_ping(i)) + "\n").encode(), timeout=15)
        assert proc.returncode == 0, proc.stderr
        assert by_id(parse_rpc(proc))[i]["result"] == {}
