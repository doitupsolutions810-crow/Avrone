import asyncio, json
from types import SimpleNamespace
from pathlib import Path
import pytest
from avrone import evolve


class Runner:
    def __init__(self, ws): self.workspace = ws
    async def run(self, cmd, timeout=None):
        import subprocess
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        from avrone.sovereign import CommandResult
        return CommandResult(cmd, p.returncode, p.stdout, p.stderr)


def mkctx(tmp, repo):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp / "d")),
                           runner=Runner(str(repo)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Evolver-1", depth=0)


def repo(tmp):
    r = tmp / "repo"; (r / "pkg").mkdir(parents=True)
    (r / "pyproject.toml").write_text(
        '[project]\nname="pkg"\nversion="0"\n[project.optional-dependencies]\ndev=["pytest"]\n'
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')
    (r / "pkg" / "__init__.py").write_text("VALUE = 1\n")
    (r / "test_v.py").write_text("from pkg import VALUE\n\ndef test_v():\n    assert VALUE == 1\n")
    import subprocess
    for c in ("git init -q", "git add -A", "git -c user.email=a@a -c user.name=a commit -qm base"):
        subprocess.run(["bash", "-c", f"cd {r} && {c}"], check=True)
    return r


def run(c): return asyncio.run(c)


def test_reject_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo(tmp_path)))
    c = mkctx(tmp_path, tmp_path / "repo")
    out = run(evolve.t_evolve_propose(c, {"goal": "break it", "edits": [{"path": "pkg/__init__.py", "content": "VALUE = 2\n"}]}))
    assert "REJECTED" in out
    assert "no proposal" not in run(evolve.t_evolve_list(c, {}))
    assert "FAIL" in run(evolve.t_evolve_list(c, {}))


def test_accept_passing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo(tmp_path)))
    c = mkctx(tmp_path, tmp_path / "repo")
    out = run(evolve.t_evolve_propose(c, {"goal": "add helper", "edits": [
        {"path": "pkg/util.py", "content": "def twice(x):\n    return x * 2\n"}]}))
    assert "ACCEPTED" in out
    pid = out.split()[1].rstrip(":")
    assert "twice" in run(evolve.t_evolve_diff(c, {"pid": pid}))
    assert evolve.list_accepted(c.core.settings)[0]["goal"] == "add helper"


def test_path_escape_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo(tmp_path)))
    c = mkctx(tmp_path, tmp_path / "repo")
    out = run(evolve.t_evolve_propose(c, {"goal": "escape", "edits": [{"path": "../evil.py", "content": "x"}]}))
    assert "escapes repo" in out

def test_path_escape_rejects_sibling_with_worktree_prefix(tmp_path):
    wt = tmp_path / "worktree"
    sibling = tmp_path / "worktree-escape"
    wt.mkdir()
    sibling.mkdir()
    (sibling / "evil.py").write_text("DO_NOT_TOUCH = True\\n")
    with pytest.raises(ValueError, match="escapes repo"):
        evolve._in_repo(wt, "../worktree-escape/evil.py")


def test_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    c = mkctx(tmp_path, tmp_path / "repo")
    assert "disabled" in run(evolve.t_evolve_propose(c, {"goal": "g", "edits": [{"path": "a", "content": "b"}]}))


def test_parallel_ids_unique_and_install_untouched(tmp_path, monkeypatch):
    import avrone
    before = avrone.__file__
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo(tmp_path)))
    c = mkctx(tmp_path, tmp_path / "repo")
    async def both():
        e = [{"path": "pkg/a.py", "content": "A = 1\n"}]
        return await asyncio.gather(evolve.t_evolve_propose(c, {"goal": "a", "edits": e}),
                                    evolve.t_evolve_propose(c, {"goal": "b", "edits": e}))
    outs = run(both())
    ids = {o.split()[1] for o in outs}
    assert len(ids) == 2 and all("ACCEPTED" in o for o in outs)
    import subprocess, sys
    now = subprocess.run([sys.executable, "-c", "import avrone;print(avrone.__file__)"],
                         capture_output=True, text=True).stdout.strip()
    assert now == before
