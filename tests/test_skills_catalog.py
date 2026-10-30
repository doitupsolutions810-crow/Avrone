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
    assert {"test-coverage-sweep", "incident-triage", "self-heal-environment",
            "privileged-dispatch-gateway"} <= names


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



def test_node_runtime_skill_documents_safe_internal_service_lifecycle():
    path = SKILLS / "agent-node-runtime" / "SKILL.md"
    _, body = agentkit.parse_skill(path.read_text(encoding="utf-8"))
    for tool in ("runtime_preflight", "node_runtime", "node_service", "agent_home", "fs_write", "agent_pack_export"):
        assert tool in body
    assert "127.0.0.1:8765" in body
    assert "No Docker host port is published" in body
    assert "do not install packages" in body



def test_node_agent_image_contains_python_and_node_runtime_checks():
    dockerfile = (SKILLS.parent / "infra" / "docker" / "agent-node" / "Dockerfile").read_text()
    assert "FROM node:22-bookworm-slim AS node_runtime" in dockerfile
    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "node --version" in dockerfile and "npm --version" in dockerfile and "python --version" in dockerfile
    assert 'CMD ["sleep", "infinity"]' in dockerfile
    assert "--privileged" not in dockerfile
    assert "EXPOSE " not in dockerfile and " -p " not in dockerfile



def test_node_agent_runtime_documentation_describes_no_host_port_and_privilege_escalation():
    doc = (SKILLS.parent / "infra" / "docker" / "agent-node" / "README.md").read_text(encoding="utf-8")
    assert "127.0.0.1:8765" in doc
    assert "does **not** publish the port to the host" in doc
    assert "does not enable privileged mode" in doc


def test_privileged_dispatch_skill_requires_pairwise_keys_and_keeps_root_scoped():
    skill = (SKILLS / "privileged-dispatch-gateway" / "SKILL.md").read_text(encoding="utf-8")
    _, body = agentkit.parse_skill(skill)
    assert "AVRONE_PRIVILEGED_PEER_KEYS" in body
    assert "AVRONE_PRIVILEGED_NODES" in body
    assert "32 UTF-8 bytes" in body
    assert "not automatically host root" in body
    assert "secret manager" in body
