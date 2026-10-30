"""Round 11 skills (state-recovery, toolset-hardening): valid, every tool and env var named is real,
and the recovery path they describe works end to end on a corrupted node."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import evolve
from avrone.fleet import FleetManager
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {
    "state-recovery": ["state_health", "evolve_list", "evolve_status", "evolve_reset", "fleet_status",
                       "memory_peek", "bridge_read", "evolve_start", "research_note"],
    "toolset-hardening": ["registry_audit", "inspect_tool", "evolve_propose", "evolve_start", "evolve_status",
                          "evolve_cancel", "evolve_diff", "evolve_reset", "research_note", "lesson_forget"],
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


def test_recovery_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "t")
    reg = builtin_registry()
    register_extra_tools(reg)
    d = tmp_path / "d"
    (d / "evolve").mkdir(parents=True)
    (d / "fleet.json").write_text("{torn")
    (d / "evolve" / "half.json").write_text('{"pid": "half"}')
    FleetManager(MagicMock(), d)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(d), workspace=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None),
                           runner=SimpleNamespace(workspace=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="r", name="Fixer-1", depth=0)

    def call(n, **a):
        return asyncio.run(reg.call(ctx, n, a))
    out = call("state_health")
    assert "ATTENTION" in out and "set aside: fleet.json.corrupt-" in out
    assert "half" not in call("evolve_list")
    assert "discarded proposal half" in call("evolve_reset", pid="half")
    out = call("state_health")
    assert "0 bad file(s)" in out and "evolve proposals: 0 file(s)" not in out
