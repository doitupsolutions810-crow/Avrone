"""Round 15 skills: load-limits and log-retention are valid, grounded in real tools and env vars, and
the numbers they quote match the code."""
import re
from pathlib import Path

import pytest

from avrone import evolve, mcp_server, server
from avrone import tools_limits as tl
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"load-limits": ["limits_status", "exposure_check", "research_note"],
          "log-retention": ["run_logs", "secret_scan", "state_health", "codex_stats", "research_note"]}


def body(n):
    return (ROOT / "skills" / n / "SKILL.md").read_text()


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_valid_and_grounded(name):
    b = body(name)
    assert evolve.check_skill(name, b) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in SKILLS[name]:
        assert t in b and reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", b)):
        assert env in src, env


def test_load_limits_maps_every_cap():
    b = body("load-limits")
    for env, *_ in tl._limits():
        if env != "AVRONE_SPAWN_CONTEXT_CHARS":
            assert env in b, env
    assert "429" in b and "413" in b and "busy" in b


def test_log_retention_defaults_match_code():
    b = body("log-retention")
    assert "default 30" in b and "default 50" in b and "fewer than 10" in b
    assert tl._int(None, 30, 1) == 30 and tl._int(3, 50, 10) == 10
