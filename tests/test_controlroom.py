import copy
import json

import pytest
from fastapi.testclient import TestClient

from avrone.controlroom import release_gates, snapshot

SHA = "a" * 40


def ci(**kw):
    value = {"name": "CI", "headSha": SHA, "databaseId": 10, "createdAt": "2026-11-11T10:00:00Z", "attempt": 1,
             "status": "completed", "conclusion": "success", "evidenceComplete": True,
             "jobs": [{"name": f"test ({version})", "status": "completed", "conclusion": "success"} for version in ("3.11", "3.12")]}
    return {**value, **kw}


def test_python_requires_exact_named_completed_jobs():
    gates = release_gates(SHA, [ci()])
    assert [g["state"] for g in gates[:2]] == ["verified", "verified"]
    assert all(g["state"] == "unverified" for g in gates[2:])


@pytest.mark.parametrize("sha", [None, "", "a" * 7, "a" * 39, "a" * 41, "G" * 40, "A" * 40, "../" + "a" * 37])
def test_invalid_shas_never_pass(sha):
    assert all(g["state"] == "unverified" for g in release_gates(sha, [ci(headSha=sha)]))


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "startup_failure"])
def test_workflow_failures_block_both_python_gates(conclusion):
    assert all(g["state"] == "blocked" for g in release_gates(SHA, [ci(conclusion=conclusion)])[:2])


@pytest.mark.parametrize("change", [
    {"headSha": "b" * 40}, {"name": "unrelated"}, {"jobs": []}, {"evidenceComplete": False},
    {"status": "queued"}, {"conclusion": "skipped"}, {"conclusion": "neutral"},
    {"jobs": [{"name": "test (3.11)", "status": "completed", "conclusion": "skipped"}]},
    {"jobs": [{"name": "test (3.11)", "status": "in_progress", "conclusion": "success"}]},
    {"jobs": [{"name": "not test (3.11)", "status": "completed", "conclusion": "success"}]},
])
def test_incomplete_or_mismatched_evidence_never_passes(change):
    assert not any(g["state"] == "verified" for g in release_gates(SHA, [ci(**change)]))


def test_latest_failed_run_supersedes_historical_success():
    assert release_gates(SHA, [ci(), ci(databaseId=11, conclusion="failure")])[0]["state"] == "blocked"


def test_later_pending_run_supersedes_historical_success():
    later = ci(databaseId=11, createdAt="2026-11-12T00:00:00Z", status="queued", conclusion="")
    assert release_gates(SHA, [ci(), later])[0]["state"] == "unverified"


def test_duplicate_named_jobs_are_ambiguous():
    run = ci()
    run["jobs"].append(copy.deepcopy(run["jobs"][0]))
    assert release_gates(SHA, [run])[0]["state"] == "unverified"


def test_artifact_names_do_not_verify_contents_or_runtime():
    run = ci(artifacts=[{"name": name, "expired": False} for name in ("coverage.xml", "coverage.json", "docker", "node")])
    assert all(g["state"] == "unverified" for g in release_gates(SHA, [run])[2:])


@pytest.mark.parametrize("merged,draft", [(False,False),(False,True),(True,False),(True,True)])
def test_merge_history_stays_independent(tmp_path, merged, draft):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schemaVersion":1,"repository":{"visibility":"PUBLIC"},"runs":[ci()],"pullRequests":[{"headRefOid":SHA,"mergedAt":"date" if merged else None,"isDraft":draft}]}))
    pr = snapshot(path)["pullRequests"][0]
    assert pr["mergeState"] == ("merged" if merged else "unmerged")
    assert pr["verification"] == "unverified"
    assert pr["releaseReady"] is False
    assert pr["nextGate"] == "Coverage artifacts"


@pytest.mark.parametrize("visibility", ["PRIVATE", "INTERNAL", "", None])
def test_private_snapshot_not_publicly_exposed(tmp_path, visibility):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schemaVersion":1,"repository":{"visibility":visibility}}))
    with pytest.raises(ValueError):
        snapshot(path)


def test_control_room_pages_and_public_evidence():
    from avrone.server import app
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.get("/").status_code == 200
        assert "Every change. Every gate." in client.get("/").text
        assert client.get("/chat").status_code == 200
        evidence = client.get("/api/control-room/repository")
        assert evidence.status_code == 200
        assert evidence.json()["repository"]["visibility"] == "PUBLIC"
        assert client.get("/api/control-room/runtime").status_code == 401
        assert client.get("/api/control-room/runtime", headers={"x-admin-token":"wrong"}).status_code == 401
        assert client.get("/api/control-room/runtime", headers={"x-admin-token":"test-token"}).status_code == 200


def test_missing_snapshot_returns_unavailable(monkeypatch):
    from avrone import controlroom
    from avrone.server import app
    def missing():
        raise FileNotFoundError()
    monkeypatch.setattr(controlroom, "snapshot", missing)
    with TestClient(app) as client:
        assert client.get("/api/control-room/repository").status_code == 503
