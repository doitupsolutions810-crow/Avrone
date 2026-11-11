"""Round 6: seeded + property fuzz of the MCP stdio server.

Random byte streams (and mixed valid/garbage frames) must never hang the
server, never emit a non-JSON / non-JSON-RPC line, and must always finish
under a short reap cap. Deterministic: fixed seeds + derandomized hypothesis.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import random
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from avrone import mcp_server as ms
from avrone.mcp_server import _ok_rpc_id


class FakeTools:
    def list(self):
        return [{"name": "echo", "source": "builtin", "args": "{}", "description": "echo"}]

    def get(self, name):
        return None


class FakeCore:
    def __init__(self):
        self.tools = FakeTools()

    async def squad(self, task, size=None):
        return {"answer": f"squad:{task}", "run_id": "r1", "agents": [1]}

    async def chat(self, task):
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r2"}


def _server():
    async def factory():
        return FakeCore(), {}
    return ms.AvroneMCPServer(core_factory=factory)


async def _feed(chunks: list[bytes], timeout: float = 2.5) -> list[str]:
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    for c in chunks:
        if c:
            reader.feed_data(c)
    reader.feed_eof()
    out = io.BytesIO()
    await asyncio.wait_for(_server().serve(reader, out), timeout)
    return out.getvalue().decode("utf-8", "replace").splitlines()


def _assert_rpc_lines(lines: list[str]) -> None:
    for line in lines:
        if not line.strip():
            continue
        msg = json.loads(line)
        assert isinstance(msg, dict)
        assert msg.get("jsonrpc") == "2.0"
        assert "result" in msg or "error" in msg
        if "id" in msg:
            assert _ok_rpc_id(msg["id"]) is True
        raw = json.dumps(msg, allow_nan=False)
        assert "NaN" not in raw and "Infinity" not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 7, 13, 42, 99, 12345, 20261110])
async def test_seeded_random_bytes_never_hang_or_emit_invalid_rpc(seed: int) -> None:
    rng = random.Random(seed)
    chunks: list[bytes] = []
    for _ in range(rng.randint(0, 12)):
        kind = rng.choice(["rand", "nul", "utf8", "ws", "valid", "batch", "partial"])
        if kind == "rand":
            chunks.append(bytes(rng.randint(0, 255) for _ in range(rng.randint(1, 80))))
        elif kind == "nul":
            chunks.append(b"\x00" * rng.randint(1, 20) + b"\n")
        elif kind == "utf8":
            chunks.append("hélø 🌍".encode()[: rng.randint(1, 10)])
        elif kind == "ws":
            chunks.append(rng.choice([b"\n", b"   \n", b"\r\n", b"\t\t\n"]))
        elif kind == "valid":
            msg = {"jsonrpc": "2.0", "id": rng.choice([1, 0, "x", None]),
                   "method": rng.choice(["ping", "tools/list", "nope"])}
            chunks.append((json.dumps(msg) + "\n").encode())
        elif kind == "batch":
            chunks.append(b"[{" + b"\n")
        else:
            chunks.append(b'{"jsonrpc":"2.0","id":')
    lines = await _feed(chunks)
    _assert_rpc_lines(lines)


@pytest.mark.asyncio
async def test_all_valid_pings_in_a_noisy_stream_are_answered() -> None:
    chunks = [
        b"\x00\xffnot-json\n",
        b'{"jsonrpc":"2.0","id":"keep-me","method":"ping"}\n',
        b"   \n",
        b'{"jsonrpc":"1.0","id":2,"method":"ping"}\n',
        b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n',
    ]
    lines = await _feed(chunks)
    _assert_rpc_lines(lines)
    ids = [json.loads(l).get("id") for l in lines if l.strip()]
    assert "keep-me" in ids


HYP = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

blob = st.binary(min_size=0, max_size=120)
chunk_list = st.lists(blob, max_size=8)


@HYP
@given(chunk_list)
def test_property_random_chunks_never_hang_or_emit_invalid_rpc(chunks: list[bytes]) -> None:
    lines = asyncio.run(_feed(list(chunks), timeout=2.5))
    _assert_rpc_lines(lines)


ROOT = Path(__file__).resolve().parents[2]


def _sub_env(tmp_path: Path) -> dict[str, str]:
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


@pytest.mark.asyncio
async def test_subprocess_fuzz_stream_exits_under_reap_cap(tmp_path: Path) -> None:
    """End-to-end: a child avrone.mcp_server fed a seeded garbage stream must
    exit promptly when stdin closes (no hang past the reap cap)."""
    (tmp_path / "ws").mkdir()
    (tmp_path / "data").mkdir()
    rng = random.Random(2026)
    payload = bytearray()
    for _ in range(20):
        payload.extend(bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 40))))
        if rng.random() < 0.4:
            payload.extend(b"\n")
        if rng.random() < 0.2:
            payload.extend((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n").encode())
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "avrone.mcp_server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=_sub_env(tmp_path), cwd=str(tmp_path),
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(bytes(payload)), 8)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        pytest.fail("mcp_server hung past the 8s reap cap on a fuzz stream")
    assert proc.returncode is not None
    _assert_rpc_lines(out.decode("utf-8", "replace").splitlines())
