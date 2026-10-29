"""Every shipped skill parses, and every tool it names exists in the built registry.

A tool reference is either:
  * a backtick span that is one bare identifier, e.g. `evolve_propose` (spans with spaces or
    arguments, like `python -m pytest -q`, are shell commands and are not checked), or
  * the step verb: the first word of a numbered step when it is lowercase or snake_case,
    e.g. "2. env_install it". Capitalised English verbs ("Read", "Report") are prose.
"""
import re
from pathlib import Path

import pytest

from avrone import agentkit
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

SKILLS = Path(__file__).resolve().parent.parent / "skills"
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))
STEP_RE = re.compile(r"^\s*\d+[.)]\s+([A-Za-z_][\w\-]*)")
TICK_RE = re.compile(r"`([^`\n]+)`")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@pytest.fixture(scope="module")
def tool_names():
    reg = builtin_registry()
    register_extra_tools(reg)
    return set(reg.names())


def tool_refs(body: str) -> set[str]:
    refs = {span.strip() for span in TICK_RE.findall(body) if IDENT_RE.match(span.strip())}
    for line in body.splitlines():
        m = STEP_RE.match(line)
        if m:
            word = m.group(1)
            if word[0].islower() or "_" in word:
                refs.add(word.lower())
    return refs


def test_skills_present():
    names = {f.parent.name for f in SKILL_FILES}
    assert {"test-coverage-sweep", "incident-triage", "self-heal-environment"} <= names


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_parses_with_name_and_description(path):
    fm, body = agentkit.parse_skill(path.read_text(encoding="utf-8"))
    assert fm.get("name") == path.parent.name
    assert len(fm.get("description", "")) >= 20
    assert any(STEP_RE.match(line) for line in body.splitlines()), "no numbered steps"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_tool_references_exist(path, tool_names):
    _, body = agentkit.parse_skill(path.read_text(encoding="utf-8"))
    unknown = sorted(tool_refs(body) - tool_names)
    assert not unknown, f"{path.parent.name} names tools not in the registry: {unknown}"


def test_tool_refs_extraction():
    body = "1. env_install it\n2. Read the error\n3. Port_scan: one host\n4. Use `shell` then `ip -j addr`\n5. bogus_tool x"
    assert tool_refs(body) == {"env_install", "port_scan", "shell", "bogus_tool"}


def test_new_playbooks_use_their_core_tools():
    cov = (SKILLS / "test-coverage-sweep" / "SKILL.md").read_text()
    tri = (SKILLS / "incident-triage" / "SKILL.md").read_text()
    for t in ("shell", "write_file", "evolve_propose", "env_install"):
        assert t in cov
    for t in ("python", "memory_recall", "env_install", "evolve_propose", "artifact_save"):
        assert t in tri


def test_loader_lists_shipped_skills(monkeypatch):
    from types import SimpleNamespace
    import asyncio
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(SKILLS))
    ctx = SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                          run_id="r", name="A", depth=0)
    out = asyncio.run(agentkit.t_skill_list(ctx, {}))
    assert "- incident-triage: Triage a failed" in out and "- test-coverage-sweep: Find AVRONE" in out



def test_coverage_skill_requires_measured_output():
    skill = (SKILLS / "test-coverage-sweep" / "SKILL.md").read_text(encoding="utf-8")
    _, body = agentkit.parse_skill(skill)
    assert "--cov=avrone" in body and "coverage.json" in body
    assert "Never infer coverage from a commit message" in body



def test_node_runtime_skill_is_present_and_respects_agent_boundaries():
    path = SKILLS / "agent-node-runtime" / "SKILL.md"
    fm, body = agentkit.parse_skill(path.read_text(encoding="utf-8"))
    assert fm["name"] == "agent-node-runtime"
    for tool in ("runtime_preflight", "node_runtime", "agent_home", "fs_write", "agent_pack_export"):
        assert tool in body
    assert "externally reachable" in body


def test_evolve_skill_keeps_long_missions_bounded_and_resumable():
    skill = (SKILLS / "evolve-loop-operations" / "SKILL.md").read_text(encoding="utf-8")
    assert "disable all timeouts" in skill
    assert "900 seconds" in skill
    assert "resume only an interrupted loop" in skill
