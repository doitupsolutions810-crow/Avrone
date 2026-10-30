"""Round 12 skills (surface-audit, runaway-process): valid, grounded in real tools and env vars,
and the runaway path works end to end: proc_status sees the child, cancelling the run kills it."""
import asyncio
import os
import re
import time
from pathlib import Path

import pytest

from avrone import evolve
from avrone import tools_hostview as hv
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {
    "surface-audit": ["api_surface", "capabilities_report", "router_status", "proc_status", "state_health",
                      "research_note"],
    "runaway-process": ["proc_status", "codex_search", "evolve_status", "evolve_cancel", "research_note"],
}


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_valid_and_grounded(name):
    body = (ROOT / "skills" / name / "SKILL.md").read_text()
    assert evolve.check_skill(name, body) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in SKILLS[name]:
        assert t in body and reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", body)):
        assert env in src, env


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc only")
def test_runaway_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PROC_LONG_S", "0.3")
    s = Settings()
    s.workspace = tmp_path
    runner = LocalRunner(s)

    async def go():
        t = asyncio.create_task(runner.run("exec sleep 40"))
        out = ""
        for _ in range(100):
            await asyncio.sleep(0.05)
            out = await hv.t_proc_status(None, {})
            if "sleep 40" in out and "1 running" in out:
                break
        assert "sleep 40" in out
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        for _ in range(100):
            if "sleep 40" not in await hv.t_proc_status(None, {"all": True}):
                return True
            await asyncio.sleep(0.05)
        return False
    assert asyncio.run(go())
