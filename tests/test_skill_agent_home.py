"""skills/agent-home-resilience validates and names only real tools and env vars."""
import re
from pathlib import Path

from avrone import agentkit, evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools
from tests.test_skills_catalog import tool_refs

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "agent-home-resilience" / "SKILL.md"
STEP_VERB = re.compile(r"(?m)^\s*\d+[.)]\s+([a-z_][a-z0-9_]*)\b")
SNAKE = re.compile(r"\b([a-z]+(?:_[a-z]+)+)\b")


def _tools():
    reg = builtin_registry()
    register_extra_tools(reg)
    return set(reg.names())


def test_validates_with_evolve_check_skill():
    md = SKILL.read_text(encoding="utf-8")
    assert evolve.check_skill("agent-home-resilience", md) is None
    # as a NEW proposal it would be refused only because it already exists
    assert "already exists" in evolve.check_skill("agent-home-resilience", md, ROOT)


def test_names_only_real_tools():
    _, body = agentkit.parse_skill(SKILL.read_text(encoding="utf-8"))
    tools = _tools()
    assert tool_refs(body) <= tools
    assert set(STEP_VERB.findall(body)) <= tools
    for t in ("agent_home", "fs_list", "fs_delete", "agent_pack_export", "agent_pack_import", "agent_migrate"):
        assert t in body and t in tools
    # every snake_case word that looks like a tool name is one (json keys / fields excepted)
    fields = {"used_pct", "born_here", "migrated_to", "memory_save"}
    unknown = {w for w in SNAKE.findall(body) if w not in tools and w not in fields}
    assert not unknown, unknown
    assert "memory_save" in tools


def test_names_only_real_env_vars():
    body = SKILL.read_text(encoding="utf-8")
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "avrone").glob("*.py"))
    envs = set(re.findall(r"AVRONE_[A-Z_]+[A-Z]", body))
    assert {"AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_MIGRATE", "AVRONE_THERMAL_MAX_C"} <= envs
    for e in envs:
        assert re.search(r"[\"']" + e + r"[\"']", src), f"{e} is not read anywhere in avrone/"
