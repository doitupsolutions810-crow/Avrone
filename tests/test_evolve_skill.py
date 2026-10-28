"""Bounded skill self-authoring. Offline: real git clones of a tiny temp repo, a FAKE gate command
(grep/test) -- never pytest-in-pytest."""
import asyncio
import json

import pytest

from avrone import evolve
from tests.test_evolve_loop import FakeRunner, env, git, mkctx, snapshot  # noqa: F401

NAME = "demo-skill"
BODY = "1. shell `ls` the workspace\n2. write_file a summary\n"
DESC = "Summarise the workspace contents into a short report file."
# Fake gate standing in for the suite's skill-catalog check: the skill exists and its frontmatter
# name matches its directory. No pytest is spawned.
GATE = f"test -f skills/{NAME}/SKILL.md && grep -q '^name: {NAME}$' skills/{NAME}/SKILL.md"


def run(c): return asyncio.run(c)


def test_good_skill_accepted_on_branch_only(env):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    before = snapshot(env.repo)
    r = run(evolve.evolve_skill_propose(ctx, NAME, description=DESC, body=BODY, test_cmd=GATE))
    assert r["passed"] and r["branch"] == f"evolve/{r['pid']}" and r["path"] == f"skills/{NAME}/SKILL.md"
    wt = r["worktree"]
    assert wt.startswith(str(env.tmp / "d" / "evolve"))                     # throwaway clone under data_dir
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == r["branch"]
    shown = git(wt, "show", f"HEAD:skills/{NAME}/SKILL.md")
    assert f"name: {NAME}" in shown and DESC in shown and "1. shell" in shown
    assert git(wt, "show", "--name-only", "--format=", "HEAD").split() == [f"skills/{NAME}/SKILL.md"]
    # live tree / main untouched: no skill there, same HEAD, clean, no new branches
    assert snapshot(env.repo) == before and not (env.repo / "skills").exists()
    # the gate ran via PYTHONPATH inside the clone, never in the live repo; nothing pushed
    assert any("PYTHONPATH=" in c and f"cd {wt}" in c for c, _ in runner.cmds)
    assert not any("git push" in c for c, _ in runner.cmds)
    m = json.loads((env.tmp / "d" / "evolve" / f"{r['pid']}.json").read_text())
    assert m["kind"] == "skill" and m["skill"] == NAME and m["passed"]


def test_full_content_and_tool_wrapper(env):
    ctx = mkctx(env.tmp, FakeRunner())
    md = f"---\nname: {NAME}\ndescription: {DESC}\n---\n{BODY}"
    out = run(evolve.t_evolve_skill_propose(ctx, {"name": NAME, "content": md, "test_cmd": GATE}))
    assert "ACCEPTED" in out and f"skills/{NAME}/SKILL.md" in out
    assert run(evolve.t_evolve_skill_propose(ctx, {"name": NAME})).startswith("ERROR")


def test_suite_failure_rejects_skill(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_skill_propose(ctx, NAME, description=DESC, body=BODY,
                                        test_cmd="echo 'FAILED test_skill_tool_references_exist'; false"))
    assert not r["passed"] and r["branch"] is None and "FAILED" in r["test_tail"]
    assert "REJECTED" in run(evolve.t_evolve_skill_propose(
        ctx, {"name": NAME, "description": DESC, "body": BODY, "test_cmd": "false"}))


@pytest.mark.parametrize("bad", ["../evil", "../../tmp/x", "a/b", "/etc/passwd", "..", ".", "x.md",
                                 "Evil", "a--b", "-lead", "trail-", "", "a" * 65, "skills/../pkg",
                                 "ok\nname: other", "ok\n", "sp ace", "nul\x00l"])
def test_path_escaping_or_bad_names_refused(env, bad):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    r = run(evolve.evolve_skill_propose(ctx, bad, description=DESC, body=BODY, test_cmd="true"))
    assert not r["passed"] and r["error"].startswith("ERROR")
    assert runner.cmds == []                                                # refused before any clone
    assert not (env.tmp / "evil").exists() and not (env.tmp / "x").exists()


def test_confine_to_blocks_escape_even_past_name_check(env):
    # Defence in depth: the edit mechanism itself refuses paths outside skills/.
    ctx = mkctx(env.tmp, FakeRunner())
    for path in ("skills/../pkg/__init__.py", "pkg/__init__.py", "skills", "../skills/x/SKILL.md"):
        r = run(evolve._propose_once(ctx, "g", [{"path": path, "content": "x"}], "true", confine_to="skills"))
        assert not r["passed"] and "escapes" in r["error"], path
    assert (env.repo / "pkg" / "__init__.py").read_text() == "VALUE = 1\n"


def test_oversized_skill_refused(env, monkeypatch):
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    big = BODY + "x" * (evolve.SKILL_MAX_BYTES_CAP + 1)
    r = run(evolve.evolve_skill_propose(ctx, NAME, description=DESC, body=big, test_cmd="true"))
    assert "too large" in r["error"] and runner.cmds == []
    # env can lower the ceiling but never raise it past the hard cap
    monkeypatch.setenv("AVRONE_EVOLVE_SKILL_MAX_BYTES", "100")
    r = run(evolve.evolve_skill_propose(ctx, NAME, description=DESC, body=BODY + "y" * 100, test_cmd="true"))
    assert "too large" in r["error"]
    monkeypatch.setenv("AVRONE_EVOLVE_SKILL_MAX_BYTES", "10000000")
    assert evolve.skill_max_bytes() == evolve.SKILL_MAX_BYTES_CAP == 16384


def test_malformed_skill_refused(env):
    ctx = mkctx(env.tmp, FakeRunner())
    assert "frontmatter name" in run(evolve.evolve_skill_propose(
        ctx, NAME, content=f"---\nname: other\ndescription: {DESC}\n---\n{BODY}"))["error"]
    assert "description" in run(evolve.evolve_skill_propose(ctx, NAME, description="short", body=BODY))["error"]
    assert "numbered steps" in run(evolve.evolve_skill_propose(ctx, NAME, description=DESC,
                                                               body="just prose"))["error"]


def test_existing_skill_never_overwritten(env):
    (env.repo / "skills" / NAME).mkdir(parents=True)
    (env.repo / "skills" / NAME / "SKILL.md").write_text("original")
    runner = FakeRunner()
    r = run(evolve.evolve_skill_propose(mkctx(env.tmp, runner), NAME, description=DESC, body=BODY, test_cmd="true"))
    assert "already exists" in r["error"] and runner.cmds == []
    assert (env.repo / "skills" / NAME / "SKILL.md").read_text() == "original"


def test_deny_blocks_skill_proposals(env, monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    runner = FakeRunner()
    ctx = mkctx(env.tmp, runner)
    r = run(evolve.evolve_skill_propose(ctx, NAME, description=DESC, body=BODY, test_cmd=GATE))
    assert not r["passed"] and "disabled" in r["error"]
    assert "disabled" in run(evolve.t_evolve_skill_propose(ctx, {"name": NAME, "description": DESC, "body": BODY}))
    assert runner.cmds == []
    assert not (env.tmp / "d" / "evolve").exists() or not any((env.tmp / "d" / "evolve").iterdir())


def test_registered():
    assert "evolve_skill_propose" in {n for n, *_ in evolve.EVOLVE_TOOLS}
