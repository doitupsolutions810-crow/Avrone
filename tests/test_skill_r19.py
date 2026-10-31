"""Round 19 skills: release-verification is valid and grounded; event-stream-health now covers the
subscriber cap and thread-side events, with numbering intact."""
import re
from pathlib import Path

import pytest

from avrone import evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SRC = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
TOOLS = {"release-verification": ["flake_probe", "registry_audit", "state_health", "proc_status", "limits_status",
                                  "run_logs", "research_note"],
         "event-stream-health": ["stream_status", "codex_search", "exposure_check", "research_note"]}


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


def test_stream_skill_matches_code():
    b = body("event-stream-health")
    assert "1013" in b and "code=1013" in SRC
    assert "refused" in b and "refused at the cap" in SRC
    assert "call_soon_threadsafe" in SRC and "worker threads" in b


def test_release_skill_names_real_entrypoint():
    assert "python -m avrone.server" in body("release-verification")
    assert (ROOT / "avrone" / "server.py").read_text().rstrip().endswith("main()")
