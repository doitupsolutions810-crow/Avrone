"""The edge-node-health and checkpoint-resume skills ship, load, validate, and name only real things."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit, evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
NEW = ("edge-node-health", "checkpoint-resume")


def _ctx():
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           run_id="r", name="A", depth=0)


def _text(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _source():
    return "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "avrone").glob("*.py"))


@pytest.mark.parametrize("name", NEW)
def test_skill_passes_self_evolve_validation(name):
    # check_skill is the gate evolve_skill_propose uses; repo=None skips the "already exists" rule.
    assert evolve.check_skill(name, _text(name)) is None


@pytest.mark.parametrize("name", NEW)
def test_skill_listed_and_loaded(name, monkeypatch):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(SKILLS))
    listing = asyncio.run(agentkit.t_skill_list(_ctx(), {}))
    fm, body = agentkit.parse_skill(_text(name))
    assert f"- {name}: {fm['description'][:30]}" in listing
    loaded = asyncio.run(agentkit.t_skill_load(_ctx(), {"name": name}))
    assert not loaded.startswith("ERROR") and "1. " in loaded


@pytest.mark.parametrize("name", NEW)
def test_env_vars_named_exist_in_code(name):
    src = _source()
    for var in set(re.findall(r"AVRONE_[A-Z_]+", _text(name))):
        assert var in src, f"{name} names unknown env var {var}"


def test_tools_named_exist():
    reg = builtin_registry()
    register_extra_tools(reg)
    names = set(reg.names())
    from avrone.fleet import FLEET_TOOLS
    fleet = {t[0] for t in FLEET_TOOLS}
    edge, ck = _text("edge-node-health"), _text("checkpoint-resume")
    for t in ("capabilities_report", "shell", "python", "think", "memory_save"):
        assert t in edge and t in names
    for t in ("fleet_nodes", "remote_spawn", "queue_submit"):
        assert t in edge and t in fleet
    assert "memory_save" in ck and "remote_spawn" in ck


def test_governor_api_named_in_edge_skill_exists():
    from avrone.governor import DEFAULT_THERMAL_MAX_C, THERMAL_SOFT_BAND_C, Governor
    edge = _text("edge-node-health")
    assert "is_hot()" in edge and hasattr(Governor, "is_hot") and hasattr(Governor, "read")
    assert f"default {int(DEFAULT_THERMAL_MAX_C)}" in edge and f"{int(THERMAL_SOFT_BAND_C)}C below" in edge


def test_checkpoint_rules_named_match_code():
    from avrone import checkpoint, cli, server
    ck = _text("checkpoint-resume")
    assert "--checkpoint-id" in ck and "--checkpoint-id" in Path(cli.__file__).read_text()
    for route in ("/api/checkpoints", "/api/chat"):
        assert route in ck and route in Path(server.__file__).read_text()
    assert "1-64" in ck
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.sanitize_client_id("x" * 65)
    assert checkpoint.sanitize_client_id("audit-2026-10-29")
