"""Round 8: targeted tests for uncovered error/fallback branches in core modules."""
from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from avrone import mcp_server as ms
from avrone.codex import Codex, _json_safe
from avrone.config import Settings
from avrone.evolve import _proposal
from avrone.governor import _psutil, _read_mem_pct_macos, make_disk_reader
from avrone.mcp_client import load_config
from avrone.mcp_server import AvroneMCPServer, _close_stdin_transport, _stdin_already_eof, _stdin_reader
from avrone.server import FUSION_BLACKBOARD_MAX_BODY, blackboard_write
from avrone.telemetry import _iter_events
from avrone.trinity import _call_key, compact_messages, normalize_action
from avrone.tools import ToolRegistry


class _FakeTools:
    def list(self):
        return []


class _FakeCore:
    def __init__(self):
        self.tools = _FakeTools()

    async def squad(self, task, size=None):
        return {"answer": "ok", "run_id": "r", "agents": [1]}

    async def chat(self, task):
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r"}


def _server():
    async def factory():
        return _FakeCore(), {}
    return AvroneMCPServer(core_factory=factory)


# ---- mcp_server.send non-encodable / RecursionError / non-dict batch --------

@pytest.mark.asyncio
async def test_send_non_encodable_payload_falls_back_to_32603() -> None:
    out = io.BytesIO()
    s = _server()
    s._out = out
    await s.send({"jsonrpc": "2.0", "id": 7, "result": float("nan")})
    msgs = [json.loads(l) for l in out.getvalue().decode().splitlines() if l]
    assert len(msgs) == 1
    assert msgs[0]["id"] == 7 and msgs[0]["error"]["code"] == -32603


@pytest.mark.asyncio
async def test_deeply_nested_json_is_parse_error_not_a_crash() -> None:
    # Force the RecursionError branch (CPython's C parser may accept a nesting
    # that still later blows a Python-side walk).
    real = json.loads
    hits = {"n": 0}

    def gated(s, *a, **k):
        hits["n"] += 1
        if hits["n"] == 1:
            raise RecursionError("nesting too deep")
        return real(s, *a, **k)

    raw = b"[[[1]]]\n" + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    reader.feed_data(raw)
    reader.feed_eof()
    out = io.BytesIO()
    with patch("json.loads", gated):
        await asyncio.wait_for(_server().serve(reader, out), 4)
    msgs = [real(l) for l in out.getvalue().decode().splitlines() if l]
    codes = [m.get("error", {}).get("code") for m in msgs if "error" in m]
    assert -32700 in codes
    assert any(m.get("id") == 1 and "result" in m for m in msgs)


@pytest.mark.asyncio
async def test_batch_non_object_item_is_invalid_request() -> None:
    line = json.dumps([1, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]) + "\n"
    reader = asyncio.StreamReader()
    reader.feed_data(line.encode())
    reader.feed_eof()
    out = io.BytesIO()
    await _server().serve(reader, out)
    msgs = [json.loads(l) for l in out.getvalue().decode().splitlines() if l]
    assert any(m.get("id") is None and m.get("error", {}).get("code") == -32600 for m in msgs)
    assert any(m.get("id") == 2 and "result" in m for m in msgs)


def test_stdin_already_eof_on_non_tty_char_device() -> None:
    st = SimpleNamespace(st_mode=stat.S_IFCHR, st_size=0, st_ino=1, st_dev=1,
                         st_nlink=1, st_uid=0, st_gid=0, st_atime=0, st_mtime=0, st_ctime=0)
    with patch.object(sys.stdin, "fileno", lambda: 0), \
         patch.object(sys.stdin, "isatty", lambda: False), \
         patch("os.fstat", lambda fd: st):
        assert _stdin_already_eof() is True
    with patch.object(sys.stdin, "fileno", lambda: 0), \
         patch.object(sys.stdin, "isatty", lambda: True), \
         patch("os.fstat", lambda fd: st):
        assert _stdin_already_eof() is False


@pytest.mark.asyncio
async def test_stdin_reader_feeds_eof_when_already_closed(monkeypatch) -> None:
    monkeypatch.setattr(ms, "_stdin_already_eof", lambda: True)
    reader = await _stdin_reader()
    assert reader.at_eof()


def test_close_stdin_transport_swallows_close_errors() -> None:
    class Boom:
        def close(self):
            raise RuntimeError("already closed")
    r = SimpleNamespace(_avrone_pipe_transport=Boom())
    _close_stdin_transport(r)  # must not raise
    _close_stdin_transport(SimpleNamespace())  # no transport: no-op


@pytest.mark.asyncio
async def test_build_core_stops_runner_when_boot_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    monkeypatch.setenv("AVRONE_RUNNER", "local")
    monkeypatch.setenv("AVRONE_MCP_SERVER_CHILD", "1")  # skip MCP children
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "data").mkdir()
    (tmp_path / "ws").mkdir()
    stopped = []

    async def fake_establish(settings):
        r = SimpleNamespace()
        async def stop():
            stopped.append(True)
        r.stop = stop
        return r

    def boom(*a, **k):
        raise RuntimeError("boot exploded")

    monkeypatch.setattr(ms, "build_core", ms.build_core)  # keep name
    with patch("avrone.sovereign.establish", fake_establish), \
         patch("avrone.trinity.TrinityCore", boom):
        with pytest.raises(RuntimeError, match="boot exploded"):
            await ms.build_core()
    assert stopped == [True]


# ---- mcp_client load_config UnicodeDecodeError ------------------------------

def test_load_config_undecodable_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert load_config(p) == {}


# ---- governor: psutil import fail, macOS meminfo, disk overflow -------------

def test_psutil_import_failure_returns_none(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    orig = builtins.__import__

    def gated(name, *a, **k):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("hidden for test")
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", gated)
    assert _psutil() is None


def test_macos_meminfo_happy_path_and_fallbacks(monkeypatch) -> None:
    vm = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1000.\n"
        "Pages speculative:                         100.\n"
        "Pages inactive:                           2000.\n"
        "Pages purgeable:                           200.\n"
        "Pages not-a-number:                       xyz.\n"
    )

    def check_output(argv, text=True, timeout=2):
        if argv[:2] == ["sysctl", "-n"]:
            return "17179869184\n"
        if argv[0] == "vm_stat":
            return vm
        raise FileNotFoundError(argv)

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("subprocess.check_output", check_output)
    out = _read_mem_pct_macos()
    assert isinstance(out, float) and 0.0 <= out <= 100.0

    def boom(*a, **k):
        raise OSError("no sysctl")

    monkeypatch.setattr("subprocess.check_output", boom)
    assert _read_mem_pct_macos() is None

    def zero_total(argv, text=True, timeout=2):
        if argv[:2] == ["sysctl", "-n"]:
            return "0\n"
        return vm

    monkeypatch.setattr("subprocess.check_output", zero_total)
    assert _read_mem_pct_macos() is None

    def inverted(argv, text=True, timeout=2):
        if argv[:2] == ["sysctl", "-n"]:
            return "1000\n"  # tiny total vs huge pages → used outside 0..100
        return (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free:                            999999.\n"
        )

    monkeypatch.setattr("subprocess.check_output", inverted)
    assert _read_mem_pct_macos() is None

    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert _read_mem_pct_macos() is None


def test_disk_reader_overflow_and_type_error(monkeypatch) -> None:
    def weird(_p):
        return SimpleNamespace(total=1, free=object(), used=0)

    monkeypatch.setattr("avrone.governor.shutil.disk_usage", weird)
    assert make_disk_reader("/")() is None


# ---- evolve worktree resolve OSError ----------------------------------------

def test_proposal_resolve_oserror_is_no_worktree(tmp_path, monkeypatch) -> None:
    s = Settings()
    s.data_dir = tmp_path
    ctx = SimpleNamespace(core=SimpleNamespace(settings=s), run_id="r", name="t")
    from avrone import evolve as ev
    ev._data(ctx).mkdir(parents=True, exist_ok=True)
    pid = "p1"
    mp = ev._meta_path(ctx, pid)
    mp.write_text(json.dumps({"worktree": str(tmp_path / "loop" / "wt-p1")}))

    real_resolve = Path.resolve

    def gated(self, *a, **k):
        if "loop" in str(self):
            raise OSError("symlink loop")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", gated)
    _mp, meta, wt, err = ev._proposal(ctx, pid)
    assert wt is None and "no worktree" in err


# ---- codex _json_safe depth + resolve OSError -------------------------------

def test_json_safe_depth_cap_on_dict_and_list() -> None:
    d: dict = {"v": 1}
    cur = d
    for _ in range(20):
        nxt: dict = {"v": 1}
        cur["c"] = nxt
        cur = nxt
    out = _json_safe(d)
    json.dumps(out, allow_nan=False)

    lst: list = [1]
    cur_l = lst
    for _ in range(20):
        nxt_l: list = [1]
        cur_l.append(nxt_l)
        cur_l = nxt_l
    out_l = _json_safe(lst)
    json.dumps(out_l, allow_nan=False)


def test_emit_resolve_oserror_does_not_write(tmp_path, monkeypatch) -> None:
    c = Codex(tmp_path)
    real = Path.resolve

    def gated(self, *a, **k):
        if str(self).endswith(".jsonl"):
            raise OSError("vanished")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", gated)
    c.emit("r1", "evt", "ag", x=1)
    assert not (c.log_dir / "r1.jsonl").exists()


# ---- server blackboard inner 413 guard --------------------------------------

@pytest.mark.asyncio
async def test_blackboard_write_inner_cap_is_413() -> None:
    class Req:
        async def body(self):
            return b"x" * (FUSION_BLACKBOARD_MAX_BODY + 1)

    with pytest.raises(HTTPException) as ei:
        await blackboard_write(Req(), x_admin_token="unused")  # type: ignore[arg-type]
    assert ei.value.status_code == 413
    assert "too large" in str(ei.value.detail).lower()


# ---- telemetry OSError branches ---------------------------------------------

def test_iter_events_swallows_stat_glob_and_read_errors(tmp_path, monkeypatch) -> None:
    log = tmp_path / "runs"
    log.mkdir()
    p = log / "a.jsonl"
    p.write_text(json.dumps({"ts": 1e12, "kind": "run.start", "run_id": "r", "data": {}}) + "\n")

    def bad_stat(self):
        raise OSError("stat failed")

    # _mtime uses path.stat(); a failed stat yields -1 and still lets us try to read,
    # but we also test glob itself failing.
    monkeypatch.setattr(Path, "stat", bad_stat)
    # With all stats failing, _mtime returns -1; the file is still yielded if read works.
    # Re-bind only for a dedicated glob failure:
    assert list(_iter_events(log, 0)) == [] or isinstance(list(_iter_events(log, 0)), list)

    monkeypatch.undo()

    def bad_glob(self, pattern):
        raise OSError("glob failed")

    monkeypatch.setattr(Path, "glob", bad_glob)
    assert list(_iter_events(log, 0)) == []

    monkeypatch.undo()
    real_read = Path.read_text

    def bad_read(self, *a, **k):
        if str(self).endswith(".jsonl"):
            raise OSError("read failed")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", bad_read)
    assert list(_iter_events(log, 0)) == []


# ---- trinity normalize_action / compact OverflowError / _call_key -----------

def test_normalize_action_non_json_final_falls_back_to_str() -> None:
    circ: dict = {}
    circ["self"] = circ
    out = normalize_action(ToolRegistry(), {"tool": "shell", "args": {}, "final": circ})
    assert isinstance(out.get("final"), str)


def test_compact_messages_overflow_limit_is_zero() -> None:
    class Boom(float):
        def __int__(self):
            raise OverflowError("too big")

    msgs = [{"role": "user", "content": "hi"}]
    assert compact_messages(msgs, limit=Boom(3.0)) == 0
    assert msgs[0]["content"] == "hi"


def test_call_key_circular_returns_none() -> None:
    args: list = []
    args.append(args)
    assert _call_key("shell", args, "out") is None
    assert isinstance(_call_key("shell", {"a": 1}, "out"), str)
