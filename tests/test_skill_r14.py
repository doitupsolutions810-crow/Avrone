"""Round 14 skills: secret-hygiene and mcp-client-check are valid and grounded, and the hygiene path
works end to end: an old run log with a secret is found without echoing it, fixed, and rescans clean."""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import evolve, mcp_server
from avrone import tools_secrets as ts
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"secret-hygiene": ["secret_scan", "exposure_check", "research_note"],
          "mcp-client-check": ["mcp_selftest", "router_status", "research_note"]}


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_valid_and_grounded(name):
    body = (ROOT / "skills" / name / "SKILL.md").read_text()
    assert evolve.check_skill(name, body) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in SKILLS[name]:
        assert t in body and reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", body)):
        assert env in src, env


def test_mcp_tool_names_in_skill_match_server():
    body = (ROOT / "skills" / "mcp-client-check" / "SKILL.md").read_text()
    for t in mcp_server.TOOLS:
        assert t["name"] in body
    assert "avrone-mcp" in (ROOT / "pyproject.toml").read_text()


def test_hygiene_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-LEAKED-0123456789abcdef")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "pre028.jsonl").write_text(json.dumps({"data": {"output": "key xai-LEAKED-0123456789abcdef"}}) + "\n")
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))))
    found = asyncio.run(ts.t_secret_scan(ctx, {}))
    assert "pre028.jsonl:1" in found and "LEAKED" not in found
    fixed = asyncio.run(ts.t_secret_scan(ctx, {"fix": True}))
    assert "0 line(s) still flagged" in fixed and "LEAKED" not in (runs / "pre028.jsonl").read_text()
