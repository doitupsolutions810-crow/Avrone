"""gap_scan / gap_mission: bounded, read-only coherence scan of AVRONE's own checkout (round 21)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import tools_gapsystem as tg
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

# the env rows tools_limits._limits() really returns; the fixture load-limits skill must map them
CAPS = ("AVRONE_MAX_BACKGROUND_RUNS", "AVRONE_MAX_MESSAGE_CHARS", "AVRONE_MAX_BODY_BYTES",
        "AVRONE_FLEET_MAX_NODES", "AVRONE_MAX_WS_SUBSCRIBERS", "AVRONE_REPLAY_MAX_EVENTS",
        "AVRONE_MCP_MAX_INFLIGHT", "AVRONE_MAX_AGENTS_PER_RUN", "AVRONE_SPAWN_CONTEXT_CHARS")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    reg = builtin_registry()
    register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)),
                           runner=SimpleNamespace(workspace=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, tmp=tmp_path)


def call(env, tool="gap_scan", **args):
    return run(env.reg.call(env.ctx, tool, args))


def make_repo(root, *, version="0.9.0", pyproject_version=None, readme=True,
              broken_skill=True, unknown_env=True, orphan_module=True, drop_cap=None):
    """A tiny AVRONE-like checkout with deliberate, known coherence gaps."""
    root = Path(root)
    av = root / "avrone"
    av.mkdir(parents=True, exist_ok=True)
    (av / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (av / "tools_extra.py").write_text(
        "def register_extra_tools(registry):\n"
        "    from .tools_flakeprobe import register_flakeprobe_tools\n"
        "    register_flakeprobe_tools(registry)\n")
    (av / "tools_flakeprobe.py").write_text("def register_flakeprobe_tools(registry):\n    pass\n")
    (av / "core.py").write_text("AVRONE_MAX_PARALLEL = 1\n" + "".join(f"{c} = 0\n" for c in CAPS))
    if orphan_module:
        (av / "tools_orphan.py").write_text("def register_orphan_tools(registry):\n    pass\n")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "avrone"\nversion = "{pyproject_version or version}"\n')
    if readme:
        (root / "README.md").write_text(f"## v{version.rsplit('.', 1)[0]}\n\nbody\n")
    sk = root / "skills"
    (sk / "good").mkdir(parents=True, exist_ok=True)
    (sk / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: a good grounded skill for the scan.\n---\n"
        "1. one: mind AVRONE_MAX_PARALLEL.\n2. two: check again.\n")
    (sk / "load-limits").mkdir(parents=True, exist_ok=True)
    mapped = [c for c in CAPS if c != drop_cap]
    (sk / "load-limits" / "SKILL.md").write_text(
        "---\nname: load-limits\ndescription: the caps this node enforces and how to match a refusal.\n---\n"
        "1. caps: " + ", ".join(mapped) + ".\n2. size for the host.\n")
    if broken_skill:
        (sk / "broken").mkdir()
        (sk / "broken" / "SKILL.md").write_text("---\nname: broken\n---\n1. only a name, no description.\n")
    if unknown_env:
        (sk / "stale").mkdir()
        (sk / "stale" / "SKILL.md").write_text(
            "---\nname: stale\ndescription: a skill naming an env no code reads.\n---\n"
            "1. set AVRONE_TOTALLY_UNKNOWN to 1.\n2. then stop.\n")
    return root


def test_registered(env):
    for name in ("gap_scan", "gap_mission"):
        t = env.reg.get(name)
        assert t is not None and t.source == "builtin", name
    assert env.reg.check("gap_scan", {"scope": 3})[0] == "bad_args"
    assert env.reg.check("gap_scan", {}) is None
    assert env.reg.check("gap_mission", {"topic": 5})[0] == "bad_args"


def test_all_scopes_and_text_render(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(make_repo(tmp_path / "repo")))
    rep = json.loads(call(env, format="json"))
    details = " ".join(f["detail"] for f in rep["findings"])
    scopes = {f["scope"] for f in rep["findings"]}
    assert {"skills", "env", "wiring"} <= scopes          # broken skill, stale env, orphan module
    assert "versions" not in scopes and "caps" not in scopes
    assert "AVRONE_TOTALLY_UNKNOWN" in details
    assert "register_orphan_tools" in details
    assert "broken" in details
    assert rep["scanned_files"] >= 3
    out = call(env)                                       # text form
    assert "gap_scan [all]" in out and "finding(s)" in out
    assert "- env/ungrounded" in out and "- wiring/unwired" in out


def test_clean_repo(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR",
                       str(make_repo(tmp_path / "repo", broken_skill=False, unknown_env=False,
                                     orphan_module=False)))
    out = call(env)
    assert "0 finding(s)" in out and "coherent" in out


def test_version_and_readme_drift(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR",
                       str(make_repo(tmp_path / "repo", pyproject_version="0.10.0", readme=False)))
    out = call(env, scope="versions")
    assert "0.10.0" in out and "0.9.0" in out
    assert "no '## vX.Y' release section" in out
    rep = json.loads(call(env, scope="versions", format="json"))
    assert rep["total"] == 2 and all(f["scope"] == "versions" for f in rep["findings"])


def test_caps_mapping_gap(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR",
                       str(make_repo(tmp_path / "repo", broken_skill=False, unknown_env=False,
                                     orphan_module=False, drop_cap="AVRONE_MCP_MAX_INFLIGHT")))
    out = call(env, scope="caps")
    assert "AVRONE_MCP_MAX_INFLIGHT" in out and "load-limits" in out


def test_scope_filter_and_refusals(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(make_repo(tmp_path / "repo")))
    rep = json.loads(call(env, scope="env", format="json"))
    assert rep["total"] >= 1 and all(f["scope"] == "env" for f in rep["findings"])
    assert "ERROR: scope must be one of" in call(env, scope="everything")
    monkeypatch.delenv("AVRONE_REPO_DIR", raising=False)
    assert "AVRONE_REPO_DIR" in call(env)                 # no checkout reachable
    assert "AVRONE_REPO_DIR" in call(env, tool="gap_mission")


def test_findings_bounded(env, tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo", broken_skill=False, unknown_env=True, orphan_module=False)
    sk = root / "skills"
    for i in range(5):                                    # more stale-env skills than the cap
        d = sk / f"stale{i}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: stale{i}\ndescription: stale.\n---\n1. set AVRONE_UNKNOWN_{i} to 1.\n2. stop.\n")
    monkeypatch.setenv("AVRONE_REPO_DIR", str(root))
    monkeypatch.setattr(tg, "GAP_SCAN_MAX_FINDINGS", 3)
    out = call(env, scope="env")
    assert "more finding(s) hidden" in out


def test_gap_mission_brief_and_emit(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(make_repo(tmp_path / "repo")))
    events = []
    env.ctx.core = SimpleNamespace(tools=env.reg, settings=env.ctx.core.settings,
                                   runner=env.ctx.core.runner,
                                   codex=SimpleNamespace(emit=lambda rid, kind, agent, **kw:
                                                          events.append((kind, dict(kw)))))
    out = call(env, tool="gap_mission", topic="wire the fleet")
    assert "gap mission: wire the fleet" in out
    assert "coherence scan" in out and "finding(s)" in out
    for t in ("bridge_post", "task_board", "research_note", "evolve_propose", "great_scale",
              "lesson_recall", "lesson_forget"):
        assert t in out, t
    assert events and events[0][0] == "gap.mission"
    assert events[0][1]["topic"] == "wire the fleet" and events[0][1]["total"] >= 1


def test_gap_mission_without_codex(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(make_repo(tmp_path / "repo")))
    out = call(env, tool="gap_mission")                   # no codex on the core: emit is skipped
    assert out.startswith("gap mission:")
