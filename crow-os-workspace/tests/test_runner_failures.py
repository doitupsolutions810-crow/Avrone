"""Syntax and runtime failures become fix items that only a later pass can clear."""

import subprocess
from pathlib import Path


def _commit(repo: Path, message: str) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "-m", message], cwd=repo, stdout=subprocess.DEVNULL)


def test_syntax_error_opens_a_fix_and_a_rerun_clears_it(workspace, repo: Path):
    workspace.ingest(token="")
    change = next(item for item in workspace.changes() if item["title"] == "main")
    launched = workspace.launch_round(
        title="broken",
        ref="main",
        files=["tests/test_edge_broken.py"],
        runner="python",
        change_id=change["change_id"],
        acceptance=True,
    )
    detail = workspace.execute_round(launched["id"])
    assert detail["status"] == "failed"
    assert detail["cases"][0]["name"] == "__compile__"
    assert "SyntaxError" in detail["cases"][0]["trace"]
    assert any("SyntaxError" in item["message"] for item in detail["anomalies"])
    fixes = [item for item in workspace.fixes() if item["status"] == "open"]
    assert len(fixes) == 1
    linked = workspace.link_fix(fixes[0]["id"], commit="HEAD", pr=24)
    assert linked["status"] == "linked"
    assert linked["fix_commit"]
    assert linked["fix_pr"] == 24
    assert workspace.evaluation(change["change_id"])["can_verify"] is False

    path = repo / "tests" / "test_edge_broken.py"
    path.write_text("def test_edge_broken():\n    assert True\n", encoding="utf-8")
    _commit(repo, "repair the edge file")
    workspace.ingest(token="")
    change = workspace.evaluation(change["change_id"])
    rerun = workspace.launch_round(
        title="reverify",
        ref="main",
        files=change["required_files"],
        runner="python",
        change_id=change["change_id"],
        acceptance=True,
    )
    done = workspace.execute_round(rerun["id"])
    assert done["status"] == "passed"
    assert all(item["status"] == "cleared" for item in workspace.fixes())
    evaluation = workspace.evaluation(change["change_id"])
    assert any(gate["gate_id"] == "fixes_reverified" and gate["passed"] for gate in evaluation["gates"])
    assert evaluation["can_verify"] is False
    workspace.attest_gate(change["change_id"], "operator_accepts_evidence", True)
    final = workspace.verify(change["change_id"])
    assert final["can_verify"] is True
    assert "verified" in final["chips"]


def test_runtime_error_is_kept_and_ollama_unreachable_is_not_a_pass(workspace, repo: Path):
    target = repo / "tests" / "test_edge_crash.py"
    target.write_text("def test_crash():\n    raise RuntimeError('boom-trace')\n", encoding="utf-8")
    _commit(repo, "crash case")
    workspace.ingest(token="")
    launched = workspace.launch_round(
        title="crash",
        ref="main",
        files=["tests/test_edge_crash.py"],
        runner="python",
        acceptance=True,
    )
    detail = workspace.execute_round(launched["id"])
    assert detail["status"] == "failed"
    blob = "\n".join(case["trace"] for case in detail["cases"])
    assert "boom-trace" in blob or "RuntimeError" in blob
    assert workspace.fixes()

    advisory = workspace.launch_round(
        title="ollama",
        ref="main",
        files=["tests/test_ok.py"],
        runner="ollama",
    )
    ollama = workspace.execute_round(advisory["id"])
    assert ollama["status"] == "failed"
    assert any(item["message"] == "Ollama unreachable" for item in ollama["anomalies"])
    assert ollama["status"] != "passed"
