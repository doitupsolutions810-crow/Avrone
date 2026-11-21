"""GitHub pull requests and check runs. Absent tokens stay explicit."""

from __future__ import annotations

from typing import Any

import httpx


API = "https://api.github.com"


class GitHubStatus:
    def __init__(self, state: str, detail: str, pulls: list[dict[str, Any]] | None = None, checks: list[dict[str, Any]] | None = None):
        self.state = state
        self.detail = detail
        self.pulls = pulls or []
        self.checks = checks or []

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "pulls": len(self.pulls), "checks": len(self.checks)}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "crow-os-avrone-workspace",
    }


def fetch_github(owner: str, repo: str, token: str, *, client: httpx.Client | None = None) -> GitHubStatus:
    if not token:
        return GitHubStatus(
            "missing_token",
            "GITHUB_TOKEN is not set. Pull requests and CI checks are not loaded. Local git branches, commits, tests, and artifact hashes are live.",
        )
    if not owner or not repo:
        return GitHubStatus(
            "error",
            "The origin remote is not a GitHub owner/repo, so pull requests were not requested.",
        )
    own_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        pulls = _pulls(http, owner, repo, token)
        checks = _checks_for_pulls(http, owner, repo, token, pulls)
        return GitHubStatus("ok", f"Loaded {len(pulls)} pull requests and {len(checks)} check runs.", pulls, checks)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in {401, 403}:
            return GitHubStatus("error", f"GitHub API rejected the token (HTTP {code}). Pull requests and CI checks were not loaded.")
        return GitHubStatus("error", f"GitHub API returned HTTP {code}. Pull requests and CI checks were not loaded.")
    except httpx.HTTPError as exc:
        return GitHubStatus("error", f"GitHub API unreachable ({type(exc).__name__}). Pull requests and CI checks were not loaded.")
    finally:
        if own_client:
            http.close()


def _get(http: httpx.Client, url: str, token: str, params: dict[str, Any] | None = None) -> Any:
    response = http.get(url, headers=_headers(token), params=params)
    response.raise_for_status()
    return response.json()


def _pulls(http: httpx.Client, owner: str, repo: str, token: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page in range(1, 4):
        batch = _get(
            http,
            f"{API}/repos/{owner}/{repo}/pulls",
            token,
            {"state": "all", "per_page": 100, "page": page, "sort": "updated", "direction": "desc"},
        )
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            head = item.get("head") or {}
            base = item.get("base") or {}
            found.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "state": item.get("state") or "",
                    "draft": 1 if item.get("draft") else 0,
                    "merged": 1 if item.get("merged_at") else 0,
                    "head_ref": head.get("ref") or "",
                    "head_sha": head.get("sha") or "",
                    "base_ref": base.get("ref") or "",
                    "html_url": item.get("html_url") or "",
                    "updated_at": item.get("updated_at") or "",
                }
            )
        if len(batch) < 100:
            break
    return found


def _checks_for_pulls(http: httpx.Client, owner: str, repo: str, token: str, pulls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interesting: list[tuple[int | None, str]] = []
    seen: set[str] = set()
    for pull in pulls:
        if pull.get("state") != "open" and not pull.get("merged"):
            continue
        sha = pull.get("head_sha") or ""
        if not sha or sha in seen:
            continue
        seen.add(sha)
        interesting.append((pull.get("number"), sha))
        if len(interesting) >= 20:
            break
    checks: list[dict[str, Any]] = []
    for number, sha in interesting:
        payload = _get(
            http,
            f"{API}/repos/{owner}/{repo}/commits/{sha}/check-runs",
            token,
            {"filter": "latest", "per_page": 100},
        )
        for run in (payload or {}).get("check_runs") or []:
            checks.append(
                {
                    "id": str(run.get("id")),
                    "name": run.get("name") or "",
                    "status": run.get("status") or "",
                    "conclusion": run.get("conclusion") or "",
                    "sha": sha,
                    "html_url": run.get("html_url") or "",
                    "pr_number": number,
                }
            )
    return checks
