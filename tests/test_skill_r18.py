"""Round 18 skills: fleet-offboarding and flood-response are valid, grounded, and the messages they
tell an agent to look for are the ones the code really produces."""
import re
from pathlib import Path

import pytest

from avrone import evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"fleet-offboarding": ["fleet_forget", "peer_audit", "fleet_status", "replay_guard", "pack_list", "research_note"],
          "flood-response": ["limits_status", "replay_guard", "proc_status", "stream_status", "exposure_check",
                             "peer_audit", "fleet_status", "research_note"]}
SRC = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))


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
    for env in set(re.findall(r"AVRONE_[A-Z_]+", b)):
        assert env in SRC, env


@pytest.mark.parametrize("phrase", ["request body too large", "registry full", "replay cache full", "busy"])
def test_flood_messages_exist_in_code(phrase):
    assert phrase in body("flood-response") and phrase in SRC


def test_offboarding_matches_forget_behaviour():
    b = body("fleet-offboarding")
    assert "would rejoin" in SRC and "re-registers on its next heartbeat" in b
    assert "cannot rejoin" in SRC and "removal is final" in b
