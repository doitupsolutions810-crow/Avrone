"""Round 4 evolve edges: default gate command resolution and junk timeout/edits."""
from __future__ import annotations

import asyncio
import shlex
import sys
from types import SimpleNamespace

from avrone import evolve
from avrone.sovereign import CommandResult


def test_default_test_cmd_uses_quoted_sys_executable():
    """Regression: a bare `python` is often missing from PATH when the gate is
    invoked as `.venv/bin/python` without activating the venv."""
    cmd = evolve._default_test_cmd()
    assert cmd.startswith(shlex.quote(sys.executable))
    assert "-m pytest" in cmd
    assert "not integration" in cmd
    assert shlex.split(cmd)[0] == sys.executable


def _ctx(tmp_path):
    return SimpleNamespace(
        name="Lead-1",
        core=SimpleNamespace(
            settings=SimpleNamespace(data_dir=tmp_path),
            codex=SimpleNamespace(emit=lambda *a, **k: None),
        ),
    )


def test_propose_once_env_override_and_clamped_timeout(tmp_path, monkeypatch):
    """AVRONE_EVOLVE_TEST_CMD wins over the default; timeout <= 0 is clamped to 1."""
    seen = []

    async def fake_sh(ctx, cmd, cwd, timeout=None):
        seen.append({"cmd": cmd, "timeout": timeout})
        return CommandResult(cmd, 0, "ok", "")

    async def ensure(ctx, pid):
        d = tmp_path / "wt"
        d.mkdir(parents=True, exist_ok=True)
        return d, "branch"

    monkeypatch.setattr(evolve, "_sh", fake_sh)
    monkeypatch.setattr(evolve, "_ensure_worktree", ensure)
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "true")

    out = asyncio.run(evolve._propose_once(
        _ctx(tmp_path), "goal", [{"path": "README.md", "content": "x"}], timeout=0))
    assert out["passed"] is True
    assert seen and "true" in seen[0]["cmd"]
    assert sys.executable not in seen[0]["cmd"]
    assert seen[0]["timeout"] == 1


def test_propose_once_junk_timeout_falls_back(tmp_path, monkeypatch):
    seen = []

    async def fake_sh(ctx, cmd, cwd, timeout=None):
        seen.append(timeout)
        return CommandResult(cmd, 1, "fail", "")

    async def ensure(ctx, pid):
        d = tmp_path / "wt"
        d.mkdir(parents=True, exist_ok=True)
        return d, "branch"

    monkeypatch.setattr(evolve, "_sh", fake_sh)
    monkeypatch.setattr(evolve, "_ensure_worktree", ensure)
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "false")

    out = asyncio.run(evolve._propose_once(
        _ctx(tmp_path), "goal", [{"path": "a.py", "content": "x"}], timeout="nope"))
    assert out["passed"] is False
    assert seen and seen[0] == 900


def test_propose_once_malformed_edit_is_an_error_result(tmp_path, monkeypatch):
    async def ensure(ctx, pid):
        d = tmp_path / "wt"
        d.mkdir(parents=True, exist_ok=True)
        return d, "branch"

    monkeypatch.setattr(evolve, "_ensure_worktree", ensure)

    out = asyncio.run(evolve._propose_once(_ctx(tmp_path), "goal", [{"content": "no path key"}]))
    assert out["passed"] is False
    assert out["error"]
    assert out["branch"] is None
