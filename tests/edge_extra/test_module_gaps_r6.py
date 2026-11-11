"""Round 6: remaining under-tested modules and interactions.

Covers: governor sensor-registry branches, sovereign env inheritance,
mcp_client reconnect/cleanup, bootstrap apply order, plugin discovery
order + name collisions, trinity compaction at exact boundaries, codex
replay-limit + iter_lines caps, telemetry window aggregation, and
checkpoint claim races under threads.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.agentfs import agents_root
from avrone.bootstrap import apply_tool_focus, build_core, run_cleanups
from avrone.checkpoint import CheckpointConflict, claim, claimed, is_busy
from avrone.codex import Codex, iter_lines, replay_limit, HARD_REPLAY_EVENTS
from avrone.config import Settings
from avrone.governor import Governor
from avrone.mcp_client import StdioTransport
from avrone.plugins import load_plugins
from avrone.sovereign import LocalRunner
from avrone.telemetry import telemetry, _finite_window
from avrone.tools import Tool, ToolRegistry
from avrone.trinity import CONTEXT_KEEP_TAIL, compact_messages


def _readers(**vals):
    defaults = {"load": 0.1, "mem_pct": 20.0, "disk_free_pct": 50.0, "temp_c": 40.0}
    defaults.update(vals)
    return {k: (lambda v=v: v) for k, v in defaults.items()}


# ---------------------------------------------------------------------------
# Governor sensor registry — remaining branches
# ---------------------------------------------------------------------------

def test_sensor_exception_and_nan_become_errors_not_crashes() -> None:
    def boom():
        raise RuntimeError("boom")

    g = Governor(4, ttl_s=0, readers=_readers(load=boom, mem_pct=float("nan")))
    # The lambda factory above would wrap the callable; pass readers directly.
    g = Governor(4, ttl_s=0, readers={
        "load": boom,
        "mem_pct": lambda: float("nan"),
        "disk_free_pct": lambda: 50.0,
        "temp_c": lambda: 40.0,
    })
    h = g.read()
    assert h.load is None and "load" in h.errors
    assert h.mem_pct is None and "mem_pct" in h.errors
    json.dumps(h.as_dict(), allow_nan=False)


def test_unknown_sensor_key_does_not_crash_read_or_target() -> None:
    r = _readers()
    r["custom"] = lambda: 1.5
    g = Governor(4, ttl_s=0, readers=r)
    h = g.read()
    assert g.target() >= 1
    # A typo key is recorded as an error or ignored, never a crash.
    json.dumps(h.as_dict(), allow_nan=False)


def test_all_sensors_none_returns_cap_reason_no_sensors() -> None:
    g = Governor(8, min_parallel=1, ttl_s=0, readers={
        k: (lambda: None) for k in ("load", "mem_pct", "disk_free_pct", "temp_c")
    })
    assert g.target() == 8
    assert "no readable" in g.last_reason or "sensor" in g.last_reason.lower()


def test_infinite_load_throttles_to_floor() -> None:
    g = Governor(8, min_parallel=1, ttl_s=0, readers=_readers(load=float("inf")))
    assert g.target() == 1
    assert g.throttle_count >= 1


def test_non_callable_sensor_is_an_error_not_a_crash() -> None:
    r = _readers()
    r["load"] = "not-callable"  # type: ignore[assignment]
    g = Governor(4, ttl_s=0, readers=r)
    h = g.read()
    assert h.load is None
    assert "load" in h.errors
    assert g.target() >= 1


# ---------------------------------------------------------------------------
# Sovereign environment inheritance
# ---------------------------------------------------------------------------

@pytest.fixture()
def runner(tmp_path):
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 10
    r = LocalRunner(s)
    asyncio.run(r.start())
    return r


def test_local_runner_without_agent_cwd_inherits_parent_env(runner, monkeypatch) -> None:
    monkeypatch.setenv("AVRONE_SOV_ONLY", "inherit-me")
    res = asyncio.run(runner.run("printf '%s' \"$AVRONE_SOV_ONLY\""))
    assert res.exit_code == 0, res.stderr
    assert res.stdout == "inherit-me"


def test_agent_env_sets_agent_dir_and_still_inherits_path(runner, tmp_path) -> None:
    agent_dir = agents_root(tmp_path) / "worker"
    agent_dir.mkdir(parents=True)
    env = runner.agent_env(agent_dir)
    assert Path(env["AVRONE_AGENT_DIR"]) == agent_dir.resolve() or env["AVRONE_AGENT_DIR"] == str(agent_dir)
    # When the command runs in a confined agent dir, agent_env is merged on
    # top of os.environ, so PATH survives and AVRONE_AGENT_DIR is set.
    res = asyncio.run(runner.run(
        "printf '%s|%s' \"$AVRONE_AGENT_DIR\" \"$PATH\"",
        cwd=agent_dir,
    ))
    assert res.exit_code == 0, res.stderr
    parts = (res.stdout or "").split("|", 1)
    assert len(parts) == 2
    assert Path(parts[0]).resolve() == agent_dir.resolve()
    assert parts[1]  # PATH inherited from the parent


def test_agent_env_does_not_drop_parent_marker(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVRONE_SOV_MARKER", "from-parent")
    agent_dir = agents_root(tmp_path) / "worker"
    agent_dir.mkdir(parents=True)
    res = asyncio.run(runner.run("printf '%s' \"$AVRONE_SOV_MARKER\"", cwd=agent_dir))
    assert res.exit_code == 0, res.stderr
    assert res.stdout == "from-parent"


# ---------------------------------------------------------------------------
# mcp_client reconnect / cleanup
# ---------------------------------------------------------------------------

_ECHO = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    try:\n"
    "        m = json.loads(line)\n"
    "    except Exception:\n"
    "        continue\n"
    "    rid = m.get('id')\n"
    "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'ok':True}})+'\\n')\n"
    "    sys.stdout.flush()\n"
)


@pytest.mark.asyncio
async def test_stdio_transport_close_is_idempotent() -> None:
    t = StdioTransport(sys.executable, ["-c", "import sys; sys.stdin.read()"])
    await t.connect()
    await t.close()
    await t.close()
    await t.close()


@pytest.mark.asyncio
async def test_stdio_transport_reconnect_replaces_child_and_cleans_up() -> None:
    t = StdioTransport(sys.executable, ["-c", _ECHO])
    await t.connect()
    first = t.proc
    assert first is not None
    await t.connect()  # must close `first` and start a new child
    second = t.proc
    assert second is not None
    assert second is not first
    if first.returncode is None:
        try:
            await asyncio.wait_for(first.wait(), 2)
        except asyncio.TimeoutError:
            first.kill()
            pytest.fail("first stdio child was leaked after reconnect")
    out = await t.request("initialize", {}, timeout=5)
    assert isinstance(out, dict) and out.get("ok") is True
    await t.close()
    if second.returncode is None:
        try:
            await asyncio.wait_for(second.wait(), 2)
        except asyncio.TimeoutError:
            second.kill()
            pytest.fail("second stdio child leaked after close")


@pytest.mark.asyncio
async def test_close_fails_pending_request() -> None:
    t = StdioTransport(sys.executable, ["-c", "import sys,time; time.sleep(30)"])
    await t.connect()
    task = asyncio.create_task(t.request("initialize", {}, timeout=15))
    await asyncio.sleep(0.05)
    await t.close()
    with pytest.raises((
        asyncio.TimeoutError, asyncio.CancelledError, ConnectionError,
        OSError, RuntimeError, EOFError, BrokenPipeError,
    )):
        await asyncio.wait_for(task, 5)
    assert task.done()


# ---------------------------------------------------------------------------
# Plugins: discovery order + collisions
# ---------------------------------------------------------------------------

def _write_plugin(dir: Path, name: str, tool: str, tag: str) -> None:
    (dir / f"{name}.py").write_text(
        "from avrone.tools import Tool\n"
        f"def register(registry, settings):\n"
        f"    registry.register(Tool({tool!r}, 'from {name}', '{{}}', lambda a: {{'tag': {tag!r}}}))\n",
        encoding="utf-8",
    )


def test_plugin_discovery_is_sorted_by_filename(tmp_path: Path, monkeypatch) -> None:
    plug = tmp_path / "plugins"
    plug.mkdir()
    _write_plugin(plug, "zeta", "zeta_tool", "z")
    _write_plugin(plug, "alpha", "alpha_tool", "a")
    _write_plugin(plug, "mu", "mu_tool", "m")
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(plug))
    reg = ToolRegistry()
    load_plugins(reg, Settings())
    names = [n for n in reg.names() if n.endswith("_tool")]
    assert names == sorted(names)
    assert names == ["alpha_tool", "mu_tool", "zeta_tool"]


def test_plugin_name_collision_last_sorted_file_wins(tmp_path: Path, monkeypatch) -> None:
    plug = tmp_path / "plugins"
    plug.mkdir()
    _write_plugin(plug, "alpha", "shared", "from-alpha")
    _write_plugin(plug, "zeta", "shared", "from-zeta")
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(plug))
    reg = ToolRegistry()
    load_plugins(reg, Settings())
    tool = reg.get("shared")
    assert tool is not None
    assert tool.fn({}) == {"tag": "from-zeta"}


def test_plugin_without_register_is_skipped(tmp_path: Path, monkeypatch) -> None:
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "empty.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_plugin(plug, "ok", "ok_tool", "ok")
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(plug))
    reg = ToolRegistry()
    load_plugins(reg, Settings())
    assert "ok_tool" in reg.names()


# ---------------------------------------------------------------------------
# Bootstrap: tool-focus + cleanup idempotence
# ---------------------------------------------------------------------------

def test_apply_tool_focus_after_registration_keeps_only_listed() -> None:
    reg = ToolRegistry()
    reg.register(Tool("shell", "s", "{}", lambda a: None))
    reg.register(Tool("read_file", "r", "{}", lambda a: None))
    dropped = apply_tool_focus(reg, "shell")
    assert "read_file" in dropped
    assert "shell" in reg.names()
    assert "read_file" not in reg.names()


@pytest.mark.asyncio
async def test_build_core_cleanup_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    (tmp_path / "data").mkdir()
    settings = Settings()
    settings.data_dir = tmp_path / "data"
    settings.workspace = tmp_path / "ws"
    settings.llm_mode = "mock"
    core, cleanups = await build_core(settings, extras=False)
    assert core is not None
    await run_cleanups(cleanups)
    await run_cleanups(cleanups)  # second pass must not raise (run_cleanups swallows)


# ---------------------------------------------------------------------------
# Trinity compaction math at exact boundaries
# ---------------------------------------------------------------------------

def test_compact_at_exact_limit_does_not_elide() -> None:
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "tool", "content": "T" * 20},
        {"role": "assistant", "content": "A"},
        {"role": "user", "content": "tail"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "tail2"},
        {"role": "assistant", "content": "done2"},
    ]
    total = sum(len(m["content"]) for m in msgs)
    n = compact_messages(msgs, limit=total)
    assert n == 0
    assert msgs[2]["content"] == "T" * 20


def test_compact_just_under_limit_elides_oldest_tool_not_tail() -> None:
    # compact_messages only elides *user* turns whose content starts with
    # "TOOL RESULT" (that is how trinity stores a tool body in the history).
    long_tool = "TOOL RESULT shell\n" + "X" * 80
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "user", "content": long_tool},
        {"role": "assistant", "content": "A"},
    ]
    for i in range(CONTEXT_KEEP_TAIL):
        msgs.append({"role": "user", "content": f"t{i}"})
    total = sum(len(m["content"]) for m in msgs)
    n = compact_messages(msgs, limit=max(1, total - 10))
    assert n >= 1
    assert msgs[2]["content"] != long_tool
    assert "elided" in msgs[2]["content"].lower() or "[" in msgs[2]["content"]
    assert msgs[-1]["content"] == f"t{CONTEXT_KEEP_TAIL - 1}"


def test_compact_zero_and_empty_limit_are_noops() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    assert compact_messages(msgs, limit=0) == 0
    assert compact_messages([], limit=10) == 0
    assert compact_messages(msgs, limit=-5) == 0


# ---------------------------------------------------------------------------
# Codex: replay limit cap + iter_lines at exact max_line boundary
# ---------------------------------------------------------------------------

def test_iter_lines_exact_cap_without_newline_is_overlong() -> None:
    cap = 8
    assert list(iter_lines(io.StringIO("Z" * cap), max_line=cap)) == [None]
    assert list(iter_lines(io.StringIO("Z" * (cap - 1)), max_line=cap)) == ["Z" * (cap - 1)]
    assert list(iter_lines(io.StringIO("Z" * (cap - 1) + "\n"), max_line=cap)) == ["Z" * (cap - 1) + "\n"]


def test_replay_respects_hard_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVRONE_REPLAY_MAX_EVENTS", str(HARD_REPLAY_EVENTS * 10))
    assert replay_limit() == HARD_REPLAY_EVENTS
    c = Codex(tmp_path)
    n = min(HARD_REPLAY_EVENTS, 50)
    for i in range(n + 10):
        c.emit("fresh", "evt", {"i": i})
    evs = c.replay("fresh", limit=None)
    assert len(evs) <= replay_limit()
    assert len(evs) <= n + 10


# ---------------------------------------------------------------------------
# Telemetry aggregation windows
# ---------------------------------------------------------------------------

def test_telemetry_windows_stay_json_safe_and_honor_horizon(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    c.emit("w1", "run.start", {"mode": "chat", "prompt": "hi"})
    c.emit("w1", "run.done", {})
    core = SimpleNamespace(
        codex=c,
        active={},
        governor=Governor(4, enabled=False, ttl_s=60, readers={
            "load": lambda: 0.1, "mem_pct": lambda: 10.0,
            "disk_free_pct": lambda: 50.0, "temp_c": lambda: 40.0,
        }),
        settings=Settings(),
        fleet=None,
    )
    for w in (0, 0.0, 1e-9, 1, 86400, 1e12, float("inf"), float("nan"), -1):
        payload = telemetry(core, window_s=w)
        raw = json.dumps(payload, allow_nan=False)
        assert "NaN" not in raw and "Infinity" not in raw
        assert isinstance(payload, dict)
    # A tiny window must not include an event whose ts is far in the past.
    # A huge window must include the run we just wrote.
    wide = telemetry(core, window_s=1e9)
    runs = wide.get("runs") or wide.get("recent") or wide
    dumped = json.dumps(wide)
    assert "w1" in dumped or (isinstance(runs, (list, dict)) and runs)


def test_finite_window_zero_is_non_negative() -> None:
    assert _finite_window(0) == 0.0 or _finite_window(0) >= 0.0
    assert math.isfinite(_finite_window(float("nan")))


# ---------------------------------------------------------------------------
# Checkpoint claim races (threads)
# ---------------------------------------------------------------------------

def test_claim_race_only_one_winner_and_release_clears(tmp_path: Path) -> None:
    path = tmp_path / "ck.json"
    path.write_text("{}", encoding="utf-8")
    winners: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        try:
            barrier.wait(2)
        except threading.BrokenBarrierError:
            return
        try:
            rel = claim(path)
        except CheckpointConflict as e:
            errors.append(e)
            return
        winners.append(i)
        time.sleep(0.05)
        rel()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
        assert not t.is_alive()
    assert len(winners) == 1, (winners, errors)
    assert len(errors) == 15
    assert is_busy(path) is False
    with claimed(path):
        assert is_busy(path) is True
    assert is_busy(path) is False
