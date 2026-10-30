"""Round 17 skills: agent-pack-hygiene and peer-onboarding are valid, grounded, and their claims
match what the code does."""
import re
from pathlib import Path

import pytest

from avrone import agentpack, evolve
from avrone.fleet import check_node_url
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"agent-pack-hygiene": ["pack_list", "agent_pack_import", "agent_pack_export", "secret_scan",
                                 "fleet_status", "peer_audit", "research_note"],
          "peer-onboarding": ["peer_audit", "replay_guard", "fleet_status", "research_note"]}


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


def test_pack_claims():
    b = body("agent-pack-hygiene")
    assert "expands past the quota" in b and "expands past the quota" in (ROOT / "avrone" / "agentpack.py").read_text()
    assert "unexpected member" in b and agentpack.PackError


def test_peer_claims():
    assert "LAN and\n   loopback urls are fine" in body("peer-onboarding")
    check_node_url("http://192.168.0.9:8710")
    check_node_url("http://127.0.0.1:8710")
    for bad in ("http://u:p@h.lan", "http://h.lan/?q", "http://169.254.169.254"):
        with pytest.raises(ValueError):
            check_node_url(bad)
