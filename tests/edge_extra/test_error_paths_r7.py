"""Round 7: error-path completeness — exercise except/fallback branches.

Each test hits a real except or fallback in mcp_server, mcp_client, sovereign,
governor, evolve, codex, telemetry, trinity, bootstrap, config, or plugins.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.bootstrap import apply_tool_focus, run_cleanups
from avrone.codex import Codex
from avrone.config import Settings, resolve_provider, _num, _load_dotenv
from avrone.evolve import _safe_test_cmd
from avrone.governor import Governor, _env_int, _env_float, _finite, make_disk_reader
from avrone.mcp_client import MCPClient, MCPError, MCPManager, load_config
from avrone.mcp_server import _clamp_rounds, _clamp_size, max_inflight
from avrone.plugins import load_plugins
from avrone.sovereign import LocalRunner
from avrone.telemetry import node_health, fleet_health
from avrone.tools import Tool, ToolRegistry
from avrone.trinity import clamp_deep_rounds, compact_messages


def test_clamp_size_and_rounds_and_inflight_junk(monkeypatch) -> None:
    assert _clamp_size(None) is None
    assert _clamp_size(True) is None
    assert _clamp_size("nope") is None
    assert _clamp_size(0) == 1
    assert _clamp_size(99) == 8
    assert _clamp_size("3") == 3
    assert _clamp_rounds(None) is None
    assert _clamp_rounds("x") is None
    assert _clamp_rounds(99) == 5  # clamp_deep_rounds caps at MAX_DEEP_ROUNDS (5)
    assert clamp_deep_rounds(True) is None
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", "nope")
    assert 1 <= max_inflight() <= 64
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", " ")
    assert 1 <= max_inflight() <= 64
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", "9999")
    assert max_inflight() == 64


def test_resolve_provider_unknown_and_num_junk() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        resolve_provider("not-a-vendor")
    assert _num("", 7, float) == 7
    assert _num("nope", 7, float) == 7
    assert _num("inf", 7, float) == 7
    assert _num("nan", 7, float) == 7
    assert _num("  3.5  ", 7, float) == 3.5


def test_load_dotenv_missing_and_unreadable(tmp_path: Path, monkeypatch) -> None:
    _load_dotenv(tmp_path / "missing.env")  # no raise
    d = tmp_path / "adir"
    d.mkdir()
    _load_dotenv(d)  # directory is not a file
    bad = tmp_path / "nope.env"
    bad.write_text("AVRONE_DOTENV_R7=1\n")
    monkeypatch.chdir(tmp_path)
    # Unreadable: chmod 0 if we can; skip assertion of values if chmod is a no-op (root).
    try:
        bad.chmod(0)
        _load_dotenv(bad)
    finally:
        bad.chmod(0o644)


def test_plugin_import_and_register_errors_are_reports(tmp_path: Path) -> None:
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "syntax.py").write_text("def register(  # noqa: invalid\n", encoding="utf-8")
    (plug / "boom.py").write_text(
        "from avrone.tools import Tool\n"
        "def register(registry, settings):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    (plug / "ok.py").write_text(
        "from avrone.tools import Tool\n"
        "def register(registry, settings):\n"
        "    registry.register(Tool('ok_r7', 'd', '{}', lambda a: 1))\n",
        encoding="utf-8",
    )
    reg = ToolRegistry()
    reports = load_plugins(reg, Settings(), directory=plug, include_entry_points=False)
    names = {r["name"]: r for r in reports}
    assert "syntax" in names and names["syntax"]["error"]
    assert "boom" in names and "RuntimeError" in names["boom"]["error"]
    assert names["ok"]["error"] in (None, "") or not names["ok"]["error"]
    assert "ok_r7" in reg.names()
    # A failing plugin must not prevent the good one from loading.
    assert "ok_r7" in reg.names()


def test_load_config_directory_and_disabled_and_malformed(tmp_path: Path) -> None:
    assert load_config(tmp_path) == {}  # directory
    assert load_config(tmp_path / "missing.json") == {}
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "good": {"command": "true"},
            "off": {"command": "true", "disabled": True},
            "bad": "not-a-dict",
        }
    }))
    cfg = load_config(p)
    assert "good" in cfg and "off" not in cfg and "bad" not in cfg
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        load_config(bad)


@pytest.mark.asyncio
async def test_mcp_manager_start_failure_is_status_not_crash(tmp_path: Path) -> None:
    cfg = {
        "gone": {"command": sys.executable, "args": ["-c", "raise SystemExit('nope')"], "timeout": 3},
    }
    mgr = MCPManager(servers=cfg)
    reg = ToolRegistry()
    status = await mgr.start(reg)
    assert status["gone"]["connected"] is False
    assert status["gone"]["error"]
    await mgr.stop()


@pytest.mark.asyncio
async def test_mcp_manager_skips_bad_tool_names(tmp_path: Path) -> None:
    # A tiny fake stdio server that lists a tool with an illegal name plus a good one.
    script = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    m=json.loads(line)\n"
        "    mid, method = m.get('id'), m.get('method')\n"
        "    if method=='initialize':\n"
        "        r={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'t'}}\n"
        "    elif method=='tools/list':\n"
        "        r={'tools':[\n"
        "            {'name':'has a space','description':'bad','inputSchema':{}},\n"
        "            {'name':'ok_tool','description':'ok','inputSchema':{}},\n"
        "        ]}\n"
        "    elif method=='notifications/initialized':\n"
        "        continue\n"
        "    else:\n"
        "        r={}\n"
        "    if mid is not None:\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':r})+'\\n')\n"
        "        sys.stdout.flush()\n"
    )
    mgr = MCPManager(servers={"s": {"command": sys.executable, "args": ["-c", script], "timeout": 5}})
    reg = ToolRegistry()
    status = await mgr.start(reg)
    try:
        assert status["s"]["connected"] is True
        # Illegal name skipped; the good one is registered as mcp__s__ok_tool.
        names = reg.names()
        assert any(n.endswith("ok_tool") for n in names)
        assert not any("has a space" in n or "has_a_space" == n for n in names)
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_sovereign_missing_binary_is_a_result(tmp_path: Path) -> None:
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 5
    r = LocalRunner(s)
    await r.start()
    res = await r._exec(["/no/such/avrone-binary-r7"], "missing", 3, cwd=tmp_path)
    assert res.exit_code == -1
    assert "could not start" in (res.stderr or "").lower() or "FileNotFound" in (res.stderr or "")


@pytest.mark.asyncio
async def test_sovereign_cancel_reaps_child(tmp_path: Path) -> None:
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 15
    r = LocalRunner(s)
    await r.start()
    task = asyncio.create_task(r.run("sleep 30", timeout=10))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    try:
        import psutil
        kids = [p for p in psutil.Process().children(recursive=True)
                if "sleep 30" in " ".join(p.cmdline()) and p.status() != "zombie"]
        assert kids == []
    except ImportError:
        pass


def test_governor_env_and_sensor_fallbacks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", "nope")
    assert _env_int("AVRONE_MIN_PARALLEL", 3) == 3
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", "inf")
    assert _env_float("AVRONE_THERMAL_MAX_C", 85.0) == 85.0
    assert _finite("nan", 1.5) == 1.5
    assert _finite(object(), 1.5) == 1.5

    def boom(_path):
        raise OSError("vanished")

    monkeypatch.setattr("avrone.governor.shutil.disk_usage", boom)
    assert make_disk_reader(str(tmp_path))() is None

    # A governor whose *factors* path explodes still returns a cap, not a raise.
    g = Governor(6, ttl_s=0, readers={
        "load": lambda: 0.1, "mem_pct": lambda: 10.0,
        "disk_free_pct": lambda: 50.0, "temp_c": lambda: 40.0,
    })
    def bad_factors(_h):
        raise RuntimeError("math exploded")
    g.factors = bad_factors  # type: ignore[method-assign]
    assert g.target() == 6
    assert "error" in g.last_reason.lower() or "governor" in g.last_reason.lower()


def test_codex_emit_when_log_dir_vanishes(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    # Remove the runs dir after construction; emit must not raise.
    import shutil
    shutil.rmtree(c.log_dir)
    c.emit("gone", "evt", {"x": 1})  # OSError swallowed
    assert c.replay("gone") == [] or isinstance(c.replay("gone"), list)


def test_telemetry_node_and_fleet_error_fallbacks() -> None:
    # Missing attributes: node_health / fleet_health must return a dict / list, not raise.
    core = SimpleNamespace()
    nh = node_health(core)
    assert isinstance(nh, dict)
    json.dumps(nh, allow_nan=False)
    assert fleet_health(core) == []
    # fleet.enabled but health_table raises:
    core.fleet = SimpleNamespace(enabled=True, health_table=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert fleet_health(core) == []


def test_apply_tool_focus_empty_and_unknown() -> None:
    reg = ToolRegistry()
    reg.register(Tool("shell", "s", "{}", lambda a: None))
    assert apply_tool_focus(reg, "") == []
    assert apply_tool_focus(reg, "   ,  ,") == []
    # Unknown names ignored; known ones kept.
    dropped = apply_tool_focus(reg, "nope, shell")
    assert "shell" in reg.names()
    assert "nope" not in dropped


@pytest.mark.asyncio
async def test_run_cleanups_swallows_one_failure_and_runs_the_rest() -> None:
    seen: list[int] = []

    def bad():
        raise RuntimeError("cleanup-1")

    def good():
        seen.append(1)

    async def also():
        seen.append(2)

    await run_cleanups([good, bad, also])
    # Reverse order; a failure must not stop the rest.
    assert 1 in seen and 2 in seen


def test_compact_messages_non_list_and_junk_limit() -> None:
    assert compact_messages("nope", limit=10) == 0  # type: ignore[arg-type]
    assert compact_messages([{"role": "user", "content": "x"}], limit=True) == 0
    assert compact_messages([{"role": "user", "content": "x"}], limit=object()) == 0
