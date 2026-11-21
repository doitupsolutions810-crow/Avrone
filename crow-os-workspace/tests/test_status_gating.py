"""Verification stays closed until the suite and every gate actually pass."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crow_os.api import create_app
from crow_os.status import VerificationRejected, evaluate_change, verification_decision


def _change(**kwargs):
    base = {"id": "c1", "title": "feature", "sha": "abc", "contained_in_main": False, "pr_number": None}
    base.update(kwargs)
    return base


def _round(files, **kwargs):
    base = {
        "id": "r1",
        "runner": "python",
        "acceptance": True,
        "status": "passed",
        "commit_sha": "abc",
        "log_sha256": "a" * 64,
        "files": files,
    }
    base.update(kwargs)
    return base


def _file(path, status="passed", digest="b" * 64):
    return {"path": path, "status": status, "log_sha256": digest}


HUMAN = [{"gate_id": "operator_accepts_evidence", "name": "Operator accepts the recorded evidence", "passed": True}]


def test_verified_only_when_every_gate_passes():
    required = ["tests/test_ok.py"]
    syntax = {"tests/test_ok.py": {"syntax_ok": True, "syntax_error": ""}}
    passed = evaluate_change(
        _change(),
        required_files=required,
        file_syntax=syntax,
        rounds=[_round([_file("tests/test_ok.py")])],
        human_gates=HUMAN,
    )
    assert passed["can_verify"] is True
    assert "verified" in passed["chips"]
    assert "unverified" not in passed["chips"]
    assert "unmerged" in passed["chips"]
    verification_decision(passed)

    blocked = evaluate_change(
        _change(),
        required_files=required,
        file_syntax={"tests/test_ok.py": {"syntax_ok": False, "syntax_error": "line 1: unterminated"}},
        rounds=[_round([_file("tests/test_ok.py")])],
        human_gates=HUMAN,
    )
    assert blocked["can_verify"] is False
    assert "verified" not in blocked["chips"]
    assert "unverified" in blocked["chips"]
    assert "failed" in blocked["chips"]
    assert "blocked" in blocked["chips"]
    assert blocked["next_gate"]["gate_id"] == "syntax_clean"
    with pytest.raises(VerificationRejected):
        verification_decision(blocked)


def test_partial_wrong_sha_ollama_and_open_fix_cannot_verify():
    required = ["tests/a.py", "tests/b.py"]
    syntax = {path: {"syntax_ok": True, "syntax_error": ""} for path in required}
    cases = [
        _round([_file("tests/a.py")]),
        _round([_file("tests/a.py"), _file("tests/b.py")], commit_sha="other"),
        _round([_file("tests/a.py"), _file("tests/b.py")], runner="ollama", status="passed"),
        _round([_file("tests/a.py"), _file("tests/b.py")], acceptance=False),
        _round([_file("tests/a.py"), _file("tests/b.py")], status="cancelled"),
        _round([_file("tests/a.py", status="failed"), _file("tests/b.py")], status="failed"),
    ]
    for rnd in cases:
        evaluation = evaluate_change(
            _change(),
            required_files=required,
            file_syntax=syntax,
            rounds=[rnd],
            human_gates=HUMAN,
        )
        assert evaluation["can_verify"] is False
        assert "verified" not in evaluation["chips"]

    hashed_but_open_fix = evaluate_change(
        _change(),
        required_files=required,
        file_syntax=syntax,
        rounds=[_round([_file("tests/a.py"), _file("tests/b.py")])],
        fixes=[{"id": "fix-1", "file_path": "tests/a.py", "status": "linked"}],
        human_gates=HUMAN,
    )
    assert hashed_but_open_fix["can_verify"] is False
    assert hashed_but_open_fix["next_gate"]["gate_id"] == "fixes_reverified"
    assert "unmerged" in hashed_but_open_fix["chips"]

    missing_hash = evaluate_change(
        _change(),
        required_files=["tests/a.py"],
        file_syntax={"tests/a.py": {"syntax_ok": True, "syntax_error": ""}},
        rounds=[_round([{"path": "tests/a.py", "status": "passed", "log_sha256": ""}], log_sha256="")],
        human_gates=HUMAN,
    )
    assert any(gate["gate_id"] == "full_suite" and gate["passed"] for gate in missing_hash["gates"])
    assert any(gate["gate_id"] == "evidence_hashed" and not gate["passed"] for gate in missing_hash["gates"])
    assert missing_hash["can_verify"] is False


def test_human_gate_and_ci_and_merged_pull():
    required = ["tests/a.py"]
    syntax = {"tests/a.py": {"syntax_ok": True, "syntax_error": ""}}
    waiting = evaluate_change(
        _change(),
        required_files=required,
        file_syntax=syntax,
        rounds=[_round([_file("tests/a.py")])],
        human_gates=[{"gate_id": "operator_accepts_evidence", "name": "Operator", "passed": False}],
    )
    assert waiting["can_verify"] is False
    assert waiting["next_gate"]["gate_id"] == "operator_accepts_evidence"

    ci_blocked = evaluate_change(
        _change(pr_number=24),
        required_files=required,
        file_syntax=syntax,
        rounds=[_round([_file("tests/a.py")])],
        human_gates=HUMAN,
        pull={"number": 24, "draft": False, "merged": False, "state": "open"},
        checks=[{"name": "test (3.12)", "status": "completed", "conclusion": "failure"}],
    )
    assert ci_blocked["can_verify"] is False
    assert "open" in ci_blocked["chips"]
    assert "unmerged" in ci_blocked["chips"]
    assert "failed" in ci_blocked["chips"]
    assert "verified" not in ci_blocked["chips"]

    merged_unverified = evaluate_change(
        _change(pr_number=20, contained_in_main=True),
        required_files=required,
        file_syntax=syntax,
        pull={"number": 20, "draft": True, "merged": True, "state": "closed"},
        checks=None,
    )
    assert "merged" in merged_unverified["chips"]
    assert "draft" in merged_unverified["chips"]
    assert "unmerged" not in merged_unverified["chips"]
    assert "unverified" in merged_unverified["chips"]
    assert "verified" not in merged_unverified["chips"]


def test_api_rejects_forced_verification_until_the_suite_and_gates_pass(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    import subprocess

    subprocess.check_call(["git", "checkout", "-b", "feature"], cwd=repo, stdout=subprocess.DEVNULL)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "tests/test_ok.py"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "-m", "feature move"], cwd=repo, stdout=subprocess.DEVNULL)

    app = create_app(repo, tmp_path / "api.sqlite", tmp_path / "wt", ingest_on_startup=True)
    with TestClient(app) as client:
        listing = client.get("/api/changes")
        assert listing.status_code == 200
        feature = next(item for item in listing.json()["changes"] if item["title"] == "feature")
        assert "verified" not in feature["chips"]
        assert "unverified" in feature["chips"]
        denied = client.post(f"/api/changes/{feature['change_id']}/verify")
        assert denied.status_code == 409
        assert denied.json()["reasons"]
        forced = client.post(
            f"/api/changes/{feature['change_id']}/gates/full_suite",
            json={"passed": True},
        )
        assert forced.status_code == 403

        created = client.post(
            "/api/changes",
            json={"title": "single file", "ref": "feature", "required_files": ["tests/test_ok.py"]},
        )
        assert created.status_code == 200
        change_id = created.json()["change_id"]
        assert "unmerged" in created.json()["chips"]
        assert "verified" not in created.json()["chips"]

        workspace = client.app.state.workspace
        launched = workspace.launch_round(
            title="acceptance",
            ref="feature",
            files=["tests/test_ok.py"],
            runner="python",
            change_id=change_id,
            acceptance=True,
        )
        workspace.execute_round(launched["id"])
        still = client.post(f"/api/changes/{change_id}/verify")
        assert still.status_code == 409
        attest = client.post(
            f"/api/changes/{change_id}/gates/operator_accepts_evidence",
            json={"passed": True},
        )
        assert attest.status_code == 200
        recorded = client.post(f"/api/changes/{change_id}/verify")
        assert recorded.status_code == 200
        assert recorded.json()["can_verify"] is True
        assert "verified" in recorded.json()["chips"]
        assert "unmerged" in recorded.json()["chips"]

        feature_again = next(item for item in client.get("/api/changes").json()["changes"] if item["change_id"] == feature["change_id"])
        assert "verified" not in feature_again["chips"]
