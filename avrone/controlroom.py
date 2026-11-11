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
    candidates = [r for r in runs if valid and r.get("headSha") == sha and r.get("name") == "CI"]
    latest = max(candidates, key=lambda r: (r.get("createdAt", ""), r.get("databaseId", 0), r.get("attempt", 0)), default=None)
    gates = []
    for key, title, requirement in GATES:
        state, reason = "unverified", "No accepted exact-SHA evidence."
        if key in ("python311", "python312") and latest:
            version = "3.11" if key == "python311" else "3.12"
            jobs = [j for j in latest.get("jobs", []) if j.get("name") == f"test ({version})"]
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
    if raw.get("schemaVersion") != 1 or raw.get("repository", {}).get("visibility") != "PUBLIC":
        raise ValueError("Only versioned public-repository evidence may be exposed")
    for pr in raw["pullRequests"]:
        pr["gates"] = release_gates(pr.get("headRefOid"), raw["runs"])
        states = [gate["state"] for gate in pr["gates"]]
        pr["verification"] = "blocked" if "blocked" in states else "verified" if all(s == "verified" for s in states) else "unverified"
        pr["mergeState"] = "merged" if pr.get("mergedAt") else "unmerged"
        pr["releaseReady"] = pr["verification"] == "verified" and not pr.get("isDraft")
        pr["nextGate"] = next((g["title"] for g in pr["gates"] if g["state"] != "verified"), "Review and approval")
    return raw
