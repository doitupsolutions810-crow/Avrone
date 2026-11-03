"""Round 28: the evolve proposal pipeline. Covers repo/data paths with spaces (external drives),
shell injection through the goal in the commit message, a commit that fails after tests pass,
path containment (sibling-clone prefix bug, .git, the repo root, directories, NUL), null and
non-string content, write errors, the clone-failure copy fallback, and evolve_diff for a
rejected proposal."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import evolve
from avrone.sovereign import CommandResult


class Runner:
    def __init__(self, ws):
        self.workspace = ws
        self.cmds = []

    async def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout or 120)
        return CommandResult(cmd, p.returncode, p.stdout, p.stderr)


def mkrepo(base: Path) -> Path:
    r = base / "repo"; (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("VALUE = 1\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "base"]):
        subprocess.run(c, cwd=r, check=True)
    return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path / "My Passport dir"          # a space, like /Volumes/My Passport
    base.mkdir()
    repo = mkrepo(base)
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo))
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    runner = Runner(str(repo))
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(base / "data dir")), runner=runner,
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    ctx = SimpleNamespace(core=core, run_id="r1", name="Evolver-1", depth=0)
    return SimpleNamespace(ctx=ctx, repo=repo, base=base, runner=runner)


def run(c):
    return asyncio.run(c)


def propose(env, edits, goal="g", test_cmd="python -c 'import pkg'"):
    return run(evolve._propose_once(env.ctx, goal, edits, test_cmd, timeout=60))


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True).stdout


# ------------------------------------------------------------------ paths with spaces
def test_spaces_in_repo_and_data_paths(env):
    r = propose(env, [{"path": "pkg/util.py", "content": "X = 2\n"}])
    assert r["passed"] and " " in r["worktree"]
    wt = Path(r["worktree"])
    assert (wt / "pkg" / "util.py").read_text() == "X = 2\n"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"evolve/{r['pid']}"
    assert "g" in _git(wt, "log", "-1", "--format=%s")


def test_pythonpath_points_at_clone_with_spaces(env):
    r = propose(env, [{"path": "pkg/__init__.py", "content": "VALUE = 42\n"}],
                test_cmd="python -c 'import pkg, sys; sys.exit(0 if pkg.VALUE == 42 else 1)'")
    assert r["passed"]


# ------------------------------------------------------------------ commit message
@pytest.mark.parametrize("goal", ["$(touch PWNED)", "`touch PWNED`", "a\"; touch PWNED; echo \"",
                                  "it's $HOME and ${PATH}", "x" * 500 + '"', "line1\nline2", "", None])
def test_goal_never_runs_and_always_commits(env, goal):
    r = propose(env, [{"path": "pkg/n.py", "content": "N = 1\n"}], goal=goal)
    wt = Path(r["worktree"])
    assert r["passed"], r["test_tail"]
    assert not (wt / "PWNED").exists() and not (env.repo / "PWNED").exists()
    subj = _git(wt, "log", "-1", "--format=%s").strip()
    assert subj and len(subj) <= 200
    if goal and "$" in goal:
        assert "$" in subj                                   # literal, not expanded


@pytest.mark.parametrize("goal,exp", [("hi", "hi"), ("a  b\n c", "a b c"), ("", "evolve proposal"),
                                      (None, "evolve proposal"), ("$(x) `y`", "$(x) `y`")])
def test_commit_message_unit(goal, exp):
    import shlex
    assert shlex.split(evolve.commit_message(goal)) == [exp]


def test_commit_message_truncates_with_ellipsis():
    import shlex
    m = shlex.split(evolve.commit_message("y" * 300))[0]
    assert m.endswith("\u2026") and len(m) == 200


def test_commit_failure_marks_rejected(env, monkeypatch):
    real = env.runner.run

    async def flaky_commit(cmd, timeout=None):
        if " commit -q " in cmd and "init" not in cmd:
            return CommandResult(cmd, 128, "fatal: unable to write new index file\n", "")
        return await real(cmd, timeout)
    env.runner.run = flaky_commit
    r = propose(env, [{"path": "pkg/n.py", "content": "N = 1\n"}])
    assert r["passed"] is False and r["branch"] is None
    assert "commit failed" in r["test_tail"] and "unable to write" in r["test_tail"]
    meta = json.loads((Path(env.ctx.core.settings.data_dir) / "evolve" / f"{r['pid']}.json").read_text())
    assert meta["passed"] is False and "commit_error" in meta


def test_identical_edit_still_commits(env):
    r = propose(env, [{"path": "pkg/__init__.py", "content": "VALUE = 1\n"}])
    assert r["passed"]


# ------------------------------------------------------------------ containment
def test_sibling_clone_prefix_escape_refused(env):
    pid_dir = Path(env.ctx.core.settings.data_dir) / "evolve"
    with pytest.raises(ValueError):
        evolve._in_repo(pid_dir / "wt-1", "../wt-10/pkg/x.py")


@pytest.mark.parametrize("path,why", [
    (".git/config", ".git"), (".git/hooks/pre-commit", ".git"), ("pkg/../.git/config", ".git"),
    ("", "bad path"), ("   ", "bad path"), (".", "repo root"), ("pkg/..", "repo root"),
    ("../outside.py", "escapes"), ("/etc/passwd", "escapes"), ("pkg/a\x00b.py", "bad path")])
def test_bad_edit_paths_refused(env, path, why):
    r = propose(env, [{"path": path, "content": "x"}])
    assert r["passed"] is False and why in r["error"]
    assert not Path(env.ctx.core.settings.data_dir, "evolve", f"wt-{r['pid']}").exists()


def test_git_config_hook_cannot_be_planted(env):
    r = propose(env, [{"path": ".git/config", "content": "[core]\n\tfsmonitor = touch PWNED\n"}])
    assert "error" in r and not (env.repo / "PWNED").exists()


def test_directory_target_refused(env):
    r = propose(env, [{"path": "pkg", "content": "x"}])
    assert r["passed"] is False and "directory" in r["error"]


def test_null_content_refused_not_written_as_None(env):
    r = propose(env, [{"path": "pkg/z.py", "content": None}])
    assert r["passed"] is False and "no content" in r["error"]


def test_missing_path_key(env):
    r = propose(env, [{"content": "x"}])
    assert r["passed"] is False and "missing" in r["error"] and "path" in r["error"]


@pytest.mark.parametrize("edit", ["pkg/x.py", ["pkg/x.py", "x"], 7])
def test_non_object_edit_refused(env, edit):
    r = propose(env, [edit])
    assert r["passed"] is False and "object" in r["error"]


def test_dict_content_written_as_json(env):
    r = propose(env, [{"path": "conf/settings.json", "content": {"a": 1}}])
    assert r["passed"]
    assert json.loads((Path(r["worktree"]) / "conf" / "settings.json").read_text()) == {"a": 1}


def test_bytes_exact_no_translation_and_surrogates(env):
    r = propose(env, [{"path": "pkg/t.txt", "content": "a\r\nb\u00e9\ud800"}])
    assert r["passed"]
    assert (Path(r["worktree"]) / "pkg" / "t.txt").read_bytes() == b"a\r\nb\xc3\xa9?"


def test_write_error_is_a_result_and_cleans_up(env, monkeypatch):
    def boom(self, data):
        raise PermissionError("read-only file system")
    monkeypatch.setattr(Path, "write_bytes", boom)
    r = propose(env, [{"path": "pkg/w.py", "content": "x"}])
    assert r["passed"] is False and "read-only" in r["error"]
    assert not Path(env.ctx.core.settings.data_dir, "evolve", f"wt-{r['pid']}").exists()


def test_skill_confinement_still_holds(env):
    r = run(evolve._propose_once(env.ctx, "g", [{"path": "pkg/x.py", "content": "x"}], "true",
                                 confine_to="skills"))
    assert "escapes skills/" in r["error"]


# ------------------------------------------------------------------ clone fallback
def test_clone_failure_with_leftover_dir_falls_back_to_copy(env, monkeypatch):
    (env.repo / "agents").mkdir(); (env.repo / "agents" / "state.json").write_text("{}")
    (env.repo / "node_modules").mkdir(); (env.repo / "node_modules" / "big").write_text("x")
    real = env.runner.run

    async def failing_clone(cmd, timeout=None):
        if "git clone" in cmd:
            # a clone that died half-way: the target dir exists
            target = Path(env.ctx.core.settings.data_dir) / "evolve"
            for part in cmd.split("'"):
                if "/wt-" in part:
                    Path(part).mkdir(parents=True, exist_ok=True)
            return CommandResult(cmd, 128, "fatal: early EOF", "")
        return await real(cmd, timeout)
    env.runner.run = failing_clone
    r = propose(env, [{"path": "pkg/c.py", "content": "C = 1\n"}])
    wt = Path(r["worktree"])
    assert r["passed"], r.get("test_tail")
    assert (wt / "pkg" / "__init__.py").exists()
    assert not (wt / "agents").exists() and not (wt / "node_modules").exists()


# ------------------------------------------------------------------ diff
def test_diff_of_rejected_proposal_shows_its_edit(env):
    # a second repo commit, so HEAD~1..HEAD would show "OTHER" (the old, wrong answer)
    (env.repo / "other.py").write_text("OTHER = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=env.repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "two"], cwd=env.repo, check=True)
    r = propose(env, [{"path": "pkg/brand_new.py", "content": "NEW_THING = 1\n"}], test_cmd="false")
    assert r["passed"] is False
    d = run(evolve.t_evolve_diff(env.ctx, {"pid": r["pid"]}))
    assert "NEW_THING" in d and "OTHER" not in d


def test_diff_of_accepted_proposal(env):
    r = propose(env, [{"path": "pkg/__init__.py", "content": "VALUE = 9\n"}])
    d = run(evolve.t_evolve_diff(env.ctx, {"pid": r["pid"]}))
    assert "+VALUE = 9" in d and "-VALUE = 1" in d


def test_diff_falls_back_without_base(env):
    r = propose(env, [{"path": "pkg/__init__.py", "content": "VALUE = 5\n"}])
    mp = Path(env.ctx.core.settings.data_dir) / "evolve" / f"{r['pid']}.json"
    meta = json.loads(mp.read_text()); meta["base"] = "; rm -rf /"; mp.write_text(json.dumps(meta))
    d = run(evolve.t_evolve_diff(env.ctx, {"pid": r["pid"]}))
    assert "+VALUE = 5" in d and not any("rm -rf" in c for c in env.runner.cmds)


def test_meta_records_base_sha(env):
    r = propose(env, [{"path": "pkg/q.py", "content": "Q = 1\n"}])
    meta = json.loads((Path(env.ctx.core.settings.data_dir) / "evolve" / f"{r['pid']}.json").read_text())
    assert meta["base"] == _git(env.repo, "rev-parse", "HEAD").strip()
