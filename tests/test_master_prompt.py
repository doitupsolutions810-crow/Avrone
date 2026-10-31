"""MASTER_PROMPT.md is generated from the code and must stay true: every tool, skill and env it names
exists, and the committed file matches a fresh generation."""
import re
import runpy
from pathlib import Path

from avrone import __version__
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
MOD = runpy.run_path(str(ROOT / "tools" / "make_master_prompt.py"))


def test_committed_prompt_is_current():
    assert (ROOT / "MASTER_PROMPT.md").read_text(encoding="utf-8") == MOD["build"](), \
        "run: python tools/make_master_prompt.py"


def test_prompt_names_only_real_things():
    text = MOD["build"]()
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z0-9_]+", text)):
        assert env in src, env
    reg = builtin_registry()
    register_extra_tools(reg)
    names = {t["name"] for t in reg.list()}
    listed = set(re.findall(r"^\| `([a-z_0-9]+)` \|", text, re.M))
    assert listed == names
    for skill in re.findall(r"^- \*\*([a-z0-9-]+)\*\*:", text, re.M):
        assert (ROOT / "skills" / skill / "SKILL.md").exists(), skill
    assert f"v{__version__}" in text and "Hard safety lines" in text
    for f in ("tools/model_bench.py", "avrone/server.py", "avrone/mcp_server.py"):
        assert (ROOT / f).exists()
