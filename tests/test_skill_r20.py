"""Round 20 skills: log-integrity is valid and grounded; log-retention now starts from log_health;
the limits and headers they cite are the code's."""
import re
from pathlib import Path

import pytest

from avrone import codex, evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SRC = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
TOOLS = {"log-integrity": ["log_health", "codex_search", "proc_status", "state_health", "secret_scan", "research_note"],
         "log-retention": ["log_health", "run_logs", "secret_scan", "state_health", "codex_stats", "research_note"]}


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


def test_integrity_facts_match_code():
    b = body("log-integrity")
    assert "4 MB" in b and codex.MAX_LINE_BYTES == 4 * 1024 * 1024
    assert "X-Avrone-Truncated" in b and "X-Avrone-Truncated" in SRC
    assert "log-retention" in b and (ROOT / "skills" / "log-retention" / "SKILL.md").exists()
