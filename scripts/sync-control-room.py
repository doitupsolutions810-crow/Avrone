"""Capture public GitHub evidence without using credentials in the browser."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

REPO = "doitupsolutions810-crow/Avrone"


def gh(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True, timeout=90))


def capture():
    repo = gh("repo", "view", REPO, "--json", "nameWithOwner,url,visibility,defaultBranchRef")
    if repo["visibility"] != "PUBLIC":
        raise RuntimeError("Public dashboard snapshots require a public repository")
    prs = gh("pr", "list", "--repo", REPO, "--state", "all", "--limit", "100", "--json",
             "number,title,state,isDraft,headRefOid,headRefName,url,mergedAt,updatedAt,author")
    runs = gh("run", "list", "--repo", REPO, "--limit", "50", "--json",
              "databaseId,headSha,status,conclusion,name,url,createdAt,updatedAt,event,attempt")
    errors = []
    for run in runs:
        if run["name"] != "CI":
            continue
        try:
            detail = gh("api", f"repos/{REPO}/actions/runs/{run['databaseId']}/jobs?per_page=100")
            run["jobs"] = [{k: j.get(k) for k in ("name", "status", "conclusion", "html_url", "started_at", "completed_at")} for j in detail["jobs"]]
            artifacts = gh("api", f"repos/{REPO}/actions/runs/{run['databaseId']}/artifacts?per_page=100")
            run["artifacts"] = [{k: a.get(k) for k in ("id", "name", "expired", "size_in_bytes", "created_at")} for a in artifacts["artifacts"]]
            run["evidenceComplete"] = detail["total_count"] <= 100 and artifacts["total_count"] <= 100
        except (subprocess.SubprocessError, KeyError, ValueError) as exc:
            run["evidenceComplete"] = False
            errors.append({"run": run["databaseId"], "error": type(exc).__name__})
    commits = gh("api", f"repos/{REPO}/commits?per_page=30")
    return {"schemaVersion": 1, "capturedAt": datetime.now(timezone.utc).isoformat(), "repository": repo,
            "pullRequests": prs, "runs": runs,
            "commits": [{"sha": c["sha"], "title": c["commit"]["message"].splitlines()[0], "url": c["html_url"], "date": c["commit"]["committer"]["date"]} for c in commits],
            "errors": errors, "scope": "Latest 100 PRs, 50 workflow runs, 30 default-branch commits. Snapshot, not live GitHub synchronization."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "avrone" / "evidence" / "repository.json")
    args = parser.parse_args()
    snapshot = capture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=args.output.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(snapshot, file, indent=2, allow_nan=False)
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Captured {len(snapshot['pullRequests'])} PRs and {len(snapshot['runs'])} runs; {len(snapshot['errors'])} evidence retrieval errors.")
