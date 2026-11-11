"""Evidence-derived release readiness, separate from GitHub merge history."""
from __future__ import annotations

import json
from pathlib import Path
import re

SNAPSHOT = Path(__file__).parent / "evidence" / "repository.json"
GATES = (
    ("python311", "Python 3.11", "Successful exact-SHA test (3.11) job in the latest CI run."),
    ("python312", "Python 3.12", "Successful exact-SHA test (3.12) job in the latest CI run."),
    ("coverage", "Coverage artifacts", "Inspect coverage.xml and coverage.json contents and bind them to this SHA."),
    ("docker", "Docker image", "Verify an image digest built from this SHA and record a real container execution."),
    ("node", "Node lifecycle", "Record install, start, health and shutdown inside that verified container."),
    ("proposal", "Proposal acceptance", "Validate lane uniqueness, failure propagation and acceptance evidence."),
)


def release_gates(sha: str, runs: list[dict]) -> list[dict]:
    valid = isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha))
    candidates = [
        run for run in runs if valid and isinstance(run, dict)
        and run.get("headSha") == sha and run.get("name") == "CI"
    ] if isinstance(runs, list) else []

    def sort_key(run: dict) -> tuple[str, int, int]:
        created_at = run.get("createdAt")
        database_id = run.get("databaseId")
        attempt = run.get("attempt")
        return (
            created_at if isinstance(created_at, str) else "",
            database_id if isinstance(database_id, int) and not isinstance(database_id, bool) else 0,
            attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0,
        )

    latest = max(candidates, key=sort_key, default=None)
    gates = []
    for key, title, requirement in GATES:
        state, reason = "unverified", "No accepted exact-SHA evidence."
        if key in ("python311", "python312") and latest:
            version = "3.11" if key == "python311" else "3.12"
            raw_jobs = latest.get("jobs")
            jobs = [j for j in raw_jobs if isinstance(j, dict) and j.get("name") == f"test ({version})"] if isinstance(raw_jobs, list) else []
            if latest.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
                state, reason = "blocked", f"Latest CI run: {latest['conclusion']}."
            elif not latest.get("evidenceComplete"):
                reason = "CI evidence retrieval is incomplete."
            elif len(jobs) == 1 and jobs[0].get("status") == "completed" and jobs[0].get("conclusion") == "success" and latest.get("status") == "completed" and latest.get("conclusion") == "success":
                state, reason = "verified", f"Exact-SHA test ({version}) completed successfully."
            elif jobs and any(j.get("conclusion") in ("failure", "cancelled", "timed_out") for j in jobs):
                state, reason = "blocked", "Test job failed or did not complete."
            else:
                reason = "A successful completed test job is required; skipped and queued jobs do not pass."
        gates.append({"id": key, "title": title, "state": state, "reason": reason, "requirement": requirement,
                      "url": latest.get("url") if latest and key.startswith("python") else None})
    return gates


def snapshot(path: Path = SNAPSHOT) -> dict:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Repository evidence must be a JSON object")
    repository = raw.get("repository")
    pull_requests = raw.get("pullRequests")
    runs = raw.get("runs")
    if (
        type(raw.get("schemaVersion")) is not int or raw["schemaVersion"] != 1
        or not isinstance(repository, dict) or repository.get("visibility") != "PUBLIC"
        or not isinstance(pull_requests, list) or not isinstance(runs, list)
        or any(not isinstance(pr, dict) for pr in pull_requests)
    ):
        raise ValueError("Only valid, versioned public-repository evidence may be exposed")
    for pr in pull_requests:
        pr["gates"] = release_gates(pr.get("headRefOid"), raw["runs"])
        states = [gate["state"] for gate in pr["gates"]]
        pr["verification"] = "blocked" if "blocked" in states else "verified" if all(s == "verified" for s in states) else "unverified"
        pr["mergeState"] = "merged" if pr.get("mergedAt") else "unmerged"
        pr["releaseReady"] = pr["verification"] == "verified" and not pr.get("isDraft")
        pr["nextGate"] = next((g["title"] for g in pr["gates"] if g["state"] != "verified"), "Review and approval")
    return raw
