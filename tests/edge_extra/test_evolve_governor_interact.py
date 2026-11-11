"""Round 5: evolve gate command safety, reap-capped subprocess, throttling governor coexistence."""
from __future__ import annotations

import asyncio
import shlex
import sys
from types import SimpleNamespace

from avrone import evolve
from avrone.governor import Governor
from avrone.sovereign import CommandResult, LocalRunner
from avrone.config import Settings


def test_safe_test_cmd_quotes_metacharacters():
    dangerous = "true; echo INJECTED && echo PWNED"
    safe = evolve._safe_test_cmd(dangerous)
    # After re-quoting, a shell must not treat ; or && as operators.
    assert "INJECTED" in safe
    tokens = shlex.split(safe)
    assert "INJECTED" in " ".join(tokens)
    # The first token is the literal 'true;' (or similar), not the builtin `true`.
    assert tokens[0] != "true"
    # Idempotent on the default gate command.
    default = evolve._default_test_cmd()
    again = evolve._safe_test_cmd(default)
    assert shlex.split(again)[0] == sys.executable


def test_env_override_with_metacharacters_does_not_inject(tmp_path, monkeypatch):
    seen = []

    async def fake_sh(ctx, cmd, cwd, timeout=None):
        seen.append(cmd)
        return CommandResult(cmd, 0, "ok", "")

    async def ensure(ctx, pid):
        d = tmp_path / "wt"
        d.mkdir(parents=True, exist_ok=True)
        return d, "branch"

    monkeypatch.setattr(evolve, "_sh", fake_sh)
    monkeypatch.setattr(evolve, "_ensure_worktree", ensure)
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "true; echo INJECTED")

    ctx = SimpleNamespace(
        name="Lead-1",
        core=SimpleNamespace(
            settings=SimpleNamespace(data_dir=tmp_path),
            codex=SimpleNamespace(emit=lambda *a, **k: None),
            governor=Governor(8, readers={
                "load": lambda: 2.0, "mem_pct": 90.0, "disk_free_pct": 3.0, "temp_c": 99.0,
            }, ttl_s=0),
        ),
    )
    # A throttling governor is present on the core (target is the floor) and
    # must not prevent a proposal from completing.
    assert ctx.core.governor.target() == 1
    out = asyncio.run(evolve._propose_once(
        ctx, "goal", [{"path": "README.md", "content": "x"}], timeout=5))
    assert out["passed"] is True
    assert seen
    # The injection is quoted, so `echo INJECTED` is not a separate command.
    cmd = seen[0]
    assert "INJECTED" in cmd
    # Unquoted `true; echo` would contain the two-character sequence '; ' as an operator.
    assert "; echo" not in cmd and ";echo" not in cmd


def test_sh_quotes_cwd_with_spaces(tmp_path):
    """_sh must quote the worktree path so a space cannot break `cd`."""
    seen = []

    class Runner:
        async def run(self, cmd, timeout=None, cwd=None):
            seen.append(cmd)
            return CommandResult(cmd, 0, "ok", "")

    ctx = SimpleNamespace(core=SimpleNamespace(runner=Runner()))
    wt = tmp_path / "work tree"
    wt.mkdir()
    asyncio.run(evolve._sh(ctx, "true", wt, timeout=5))
    assert seen
    assert f"cd {shlex.quote(str(wt))}" in seen[0]


def test_reap_capped_subprocess_under_local_runner(tmp_path):
    """A long sleep honours the timeout and the 2s reap cap (evolve lanes share this runner)."""
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 15
    r = LocalRunner(s)
    asyncio.run(r.start())
    import time
    t0 = time.monotonic()
    res = asyncio.run(r.run(f"{sys.executable} -c 'import time; time.sleep(30)'", timeout=1))
    elapsed = time.monotonic() - t0
    assert res.timed_out is True
    assert elapsed < 8
