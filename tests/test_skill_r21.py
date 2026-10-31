"""Round 21 skills: systems-gap-bridge is valid and grounded; the tools it cites exist."""
import re
from pathlib import Path

import pytest

from avrone import evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SRC = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
TOOLS = {"systems-gap-bridge": ["gap_scan", "gap_mission", "inspect_tool", "registry_audit", "read_file",
                                "bridge_post", "task_board", "bridge_read", "bridge_reply",
                                "research_note", "lesson_recall", "lesson_forget", "evolve_propose",
                                "great_scale"]}


def body(n):
    return (ROOT / "skills" / n / "SKILL.md").read_text()


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_valid_grounded_numbered(name):
    b = body(name)
    assert evolve.check_skill(name, b) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in TOOLS[name]:
        assert t in b and reg.get(t) is not None, t
    for env in set(re.findall(r"AVRONE_[A-Z_]+", b)):
        assert env in SRC, env
    steps = [int(m) for m in re.findall(r"^(\d+)\. ", b, re.M)]
    assert steps == list(range(1, len(steps) + 1))


def test_gap_engine_wired_and_self_coherent():
    reg = builtin_registry()
    register_extra_tools(reg)
    assert reg.get("gap_scan") is not None and reg.get("gap_mission") is not None
    engine = ROOT / "avrone" / "gapsystem.py"
    assert engine.exists() and "AVRONE_REPO_DIR" in engine.read_text()
    extra = (ROOT / "avrone" / "tools_extra.py").read_text()
    assert "register_gapsystem_tools(registry)" in extra
