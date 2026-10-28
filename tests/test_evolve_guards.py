"""Guards and bounded-duration behavior for the self-evolution tools."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import evolve


def ctx(tmp_path):
    core = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path / "data"),
        codex=SimpleNamespace(emit=lambda *args, **kwargs: None),
    )
    return SimpleNamespace(core=core, run_id="guard-test", name="Guard-Agent", depth=0)


def test_in_repo_uses_path_containment_not_string_prefix(tmp_path):
    root = tmp_path / "worktree"
    sibling = tmp_path / "worktree-evil"
    root.mkdir()
    sibling.mkdir()
    safe = evolve._in_repo(root, "src/module.py")
    assert safe == root / "src" / "module.py"
    with pytest.raises(ValueError, match="escapes repo"):
        evolve._in_repo(root, "../worktree-evil/payload.py")


def test_in_repo_blocks_absolute_root_git_metadata_and_symlink_escape(tmp_path):
    root = tmp_path / "worktree"
    external = tmp_path / "outside"
    root.mkdir()
    external.mkdir()
    with pytest.raises(ValueError, match="relative"):
        evolve._in_repo(root, str(external / "file.py"))
    with pytest.raises(ValueError, match="Git metadata"):
        evolve._in_repo(root, ".git/config")
    with pytest.raises(ValueError, match="inside the repo"):
        evolve._in_repo(root, ".")
    (root / "jump").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes repo"):
        evolve._in_repo(root, "jump/payload.py")


@pytest.mark.parametrize("pid", ["", ".", "..", "../victim", "a/b", "a\\\\b", "has space"])
def test_metadata_path_rejects_untrusted_proposal_ids(tmp_path, pid):
    with pytest.raises(ValueError, match="invalid proposal id"):
        evolve._meta_path(ctx(tmp_path), pid)


def test_edit_validation_caps_count_bytes_duplicate_paths_and_types():
    with pytest.raises(ValueError, match="too many edits"):
        evolve._validate_edits([{"path": f"f{i}.py", "content": ""} for i in range(evolve.MAX_PROPOSAL_EDITS + 1)])
    with pytest.raises(ValueError, match="exceeds"):
        evolve._validate_edits([{"path": "large.py", "content": "x" * (evolve.MAX_EDIT_BYTES + 1)}])
    with pytest.raises(ValueError, match="total edit bytes"):
        evolve._validate_edits([{"path": f"f{i}.py", "content": "x" * evolve.MAX_EDIT_BYTES} for i in range(5)])
    with pytest.raises(ValueError, match="duplicate"):
        evolve._validate_edits([{"path": "src/a.py", "content": "a"}, {"path": "src/a.py", "content": "b"}])
    with pytest.raises(ValueError, match="content must be a string"):
        evolve._validate_edits([{"path": "a.py", "content": {"code": "x"}}])
    with pytest.raises(ValueError, match="forbidden component"):
        evolve._validate_edits([{"path": "../outside.py", "content": "x"}])
    with pytest.raises(ValueError, match="forbidden component"):
        evolve._validate_edits([{"path": ".git/config", "content": "x"}])


def test_proposal_timeout_obeys_operator_ceiling_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_SECONDS", "45")
    assert evolve._proposal_timeout({"duration_seconds": 90}) == 45
    assert evolve._proposal_timeout({"duration_seconds": 12}) == 12
    assert evolve._proposal_timeout({}) == 45
    for value in (0, -1, True, 1.5, "forever"):
        with pytest.raises(ValueError, match="positive integer"):
            evolve._proposal_timeout({"duration_seconds": value})


def test_test_command_accepts_pytest_and_rejects_shell_execution(monkeypatch):
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    cmd = evolve._validated_test_command({"test_cmd": 'python -m pytest -q -m "not integration"'})
    assert cmd.startswith("python -m pytest")
    assert "not integration" in cmd
    assert evolve._validated_test_command({"test_cmd": "pytest -q"}) == "pytest -q"
    for cmd in (
        "echo hi",
        "python -m pytest -q && touch /tmp/escaped",
        "python -m pytest -q; rm -rf /tmp/escaped",
        "python -m pytest $(touch /tmp/escaped)",
    ):
        with pytest.raises(ValueError):
            evolve._validated_test_command({"test_cmd": cmd})


def test_evolve_status_summarizes_outcomes_and_measured_durations(tmp_path, monkeypatch):
    c = ctx(tmp_path)
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_SECONDS", "90")
    for pid, passed, duration in (("prop-a", True, 2.5), ("prop-b", False, 7.0)):
        evolve._meta_path(c, pid).write_text(json.dumps({
            "pid": pid, "goal": "test", "passed": passed, "duration_s": duration,
            "files": ["avrone/evolve.py"], "by": "test-agent",
        }))
    out = asyncio.run(evolve.t_evolve_status(c, {}))
    assert "2 total; 1 accepted; 1 rejected" in out
    assert "9.50s total" in out and "4.75s average" in out
    assert "90s" in out and "loop status" in out


def test_reset_invalid_pid_cannot_delete_external_file(tmp_path):
    c = ctx(tmp_path)
    sentinel = tmp_path / "outside.json"
    sentinel.write_text('{"keep": true}')
    result = asyncio.run(evolve.t_evolve_reset(c, {"pid": "../outside"}))
    assert result.startswith("ERROR:")
    assert json.loads(sentinel.read_text()) == {"keep": True}


def test_list_accepted_skips_malformed_and_non_object_metadata(tmp_path):
    data = tmp_path / "data" / "evolve"
    data.mkdir(parents=True)
    (data / "broken.json").write_text("{not json")
    (data / "scalar.json").write_text("null")
    (data / "bad-shape.json").write_text(json.dumps({"passed": "yes", "pid": "not-a-boolean-pass"}))
    (data / "accepted.json").write_text(json.dumps({
        "passed": True, "pid": "p1", "goal": "ok", "branch": "evolve/p1",
        "files": ["a.py"], "by": "Lead", "ts": 1.0, "duration_s": 2.0, "timeout_s": 30,
    }))
    result = evolve.list_accepted(SimpleNamespace(data_dir=tmp_path / "data"))
    assert len(result) == 1 and result[0]["pid"] == "p1"
    assert result[0]["duration_s"] == 2.0


def test_ensure_worktree_refuses_preexisting_symlink(tmp_path, monkeypatch):
    c = ctx(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    wt = evolve._worktree_path(c, "prop-a")
    wt.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("AVRONE_REPO_DIR", str(tmp_path / "missing-repo"))
    with pytest.raises(ValueError, match="must not be a symlink"):
        asyncio.run(evolve._ensure_worktree(c, "prop-a", time.monotonic() + 30))
    assert list(outside.iterdir()) == []


def test_transient_failure_retries_the_full_suite_not_only_last_failed(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("source")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=test@example.invalid", "-c", "user.name=test", "commit", "-qm", "base"], cwd=repo, check=True)

    class ScriptedRunner:
        def __init__(self):
            self.tests = []
        async def run(self, command, timeout=None):
            if "python -m pytest" in command:
                self.tests.append(command)
                exit_code = 1 if len(self.tests) == 1 else 0
                return SimpleNamespace(exit_code=exit_code, stdout="simulated full-suite result", stderr="")
            proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
            return SimpleNamespace(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    runner = ScriptedRunner()
    c = ctx(tmp_path)
    c.core.runner = runner
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo))
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TEST_CMD", raising=False)
    out = asyncio.run(evolve.t_evolve_propose(c, {
        "goal": "prove full suite retry",
        "edits": [{"path": "README.md", "content": "changed in isolated proposal"}],
        "test_cmd": "python -m pytest -q",
        "duration_seconds": 60,
    }))
    assert "ACCEPTED" in out
    assert len(runner.tests) == 2
    assert runner.tests[0] == runner.tests[1]
    assert all("--lf" not in command for command in runner.tests)
