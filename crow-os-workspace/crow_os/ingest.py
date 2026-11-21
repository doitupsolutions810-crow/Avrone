"""Ingest committed git state and, when a token is set, GitHub pulls and checks."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import gitrepo
from .github_api import GitHubStatus, fetch_github
from .status import HUMAN_GATE_ID, HUMAN_GATE_NAME
from .util import (
    artifact_kind,
    change_id_for_branch,
    classify_suite,
    components_in_source,
    sha256_bytes,
    utc_now,
)


def resolve_token(explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _analyze_python(path: str, blob: bytes) -> dict[str, Any]:
    text = blob.decode("utf-8", errors="replace")
    components = components_in_source(text)
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return {
            "syntax_ok": 0,
            "syntax_error": f"line {exc.lineno}: {exc.msg}",
            "case_count": None,
            "components": components,
        }
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            count += 1
    return {"syntax_ok": 1, "syntax_error": "", "case_count": count, "components": components}


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def default_required(test_rows: list[dict[str, Any]]) -> list[str]:
    edge = sorted(
        row["path"]
        for row in test_rows
        if row["suite"] in {"edge", "daemon-edge"} and "edge" in row["path"].rsplit("/", 1)[-1]
    )
    if edge:
        return edge
    return sorted(row["path"] for row in test_rows if row["suite"] in {"core", "squad"})


def scan_commit(repo: Path, sha: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Return test rows and artifact rows for one commit, plus an error string."""
    try:
        entries = gitrepo.ls_tree(repo, sha)
    except gitrepo.GitError as exc:
        return [], [], str(exc)
    tests = [entry for entry in entries if entry["type"] == "blob" and _is_test_path(str(entry["path"]))]
    artifacts = []
    for entry in entries:
        if entry["type"] != "blob":
            continue
        kind = artifact_kind(str(entry["path"]))
        if kind:
            artifacts.append(entry)
    wanted = {str(entry["blob"]): entry for entry in tests + artifacts}
    try:
        blobs = gitrepo.cat_blobs(repo, list(wanted))
    except gitrepo.GitError as exc:
        return [], [], str(exc)

    test_rows = []
    for entry in tests:
        blob = blobs.get(str(entry["blob"]), b"")
        parsed = _analyze_python(str(entry["path"]), blob)
        test_rows.append(
            {
                "path": entry["path"],
                "sha256": sha256_bytes(blob),
                "size": len(blob),
                "case_count": parsed["case_count"],
                "syntax_ok": parsed["syntax_ok"],
                "syntax_error": parsed["syntax_error"],
                "suite": classify_suite(str(entry["path"])),
                "components": json.dumps(parsed["components"]),
            }
        )
    artifact_rows = []
    for entry in artifacts:
        blob = blobs.get(str(entry["blob"]), b"")
        digest = sha256_bytes(blob) if blob or entry["size"] == 0 else ""
        if entry["size"] and len(blob) != entry["size"]:
            digest = sha256_bytes(blob) if blob else ""
        artifact_rows.append(
            {
                "path": entry["path"],
                "sha256": digest,
                "size": int(entry["size"] or len(blob)),
                "kind": artifact_kind(str(entry["path"])),
            }
        )
    return test_rows, artifact_rows, None


def _deep_refs(branch_names: list[str], current: str, pull_refs: list[str]) -> list[str]:
    chosen = []
    for name in branch_names:
        if name in {current, "main", "origin/main", "origin/avrone-a12lr-daemon"}:
            chosen.append(name)
        elif name.startswith("origin/edge/"):
            chosen.append(name)
        elif name.startswith("origin/squad/round"):
            chosen.append(name)
    for name in pull_refs:
        if name not in chosen:
            chosen.append(name)
    return chosen


def _upsert_change(store: Any, row: dict[str, Any]) -> None:
    existing = store.query_one("SELECT id, required_source FROM changes WHERE id=?", (row["id"],))
    if existing is None:
        store.execute(
            """
            INSERT INTO changes
                (id, title, ref, sha, pr_number, source, required_source, required_files, contained_in_main, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["ref"],
                row["sha"],
                row.get("pr_number"),
                row["source"],
                row["required_source"],
                json.dumps(row["required_files"]),
                1 if row.get("contained_in_main") else 0,
                utc_now(),
            ),
        )
        store.execute(
            """
            INSERT OR IGNORE INTO gates (change_id, gate_id, name, kind, passed, detail)
            VALUES (?, ?, ?, 'human', 0, ?)
            """,
            (
                row["id"],
                HUMAN_GATE_ID,
                HUMAN_GATE_NAME,
                "Waiting for an operator to accept the recorded evidence.",
            ),
        )
        return
    if existing["required_source"] == "auto":
        store.execute(
            """
            UPDATE changes
               SET title=?, ref=?, sha=?, pr_number=?, source=?, required_files=?, contained_in_main=?
             WHERE id=?
            """,
            (
                row["title"],
                row["ref"],
                row["sha"],
                row.get("pr_number"),
                row["source"],
                json.dumps(row["required_files"]),
                1 if row.get("contained_in_main") else 0,
                row["id"],
            ),
        )
    else:
        store.execute(
            "UPDATE changes SET title=?, sha=?, contained_in_main=? WHERE id=?",
            (row["title"], row["sha"], 1 if row.get("contained_in_main") else 0, row["id"]),
        )


def ingest(
    store: Any,
    repo: Path,
    *,
    token: str | None = None,
    github_fetch: Callable[..., GitHubStatus] = fetch_github,
) -> dict[str, Any]:
    repo = repo.resolve()
    warnings: list[str] = []
    branch_meta = gitrepo.for_each_ref(repo)
    current = gitrepo.current_branch(repo)
    head = gitrepo.head_sha(repo)
    dirty = gitrepo.worktree_dirty(repo)
    owner_repo = None
    try:
        owner_repo = None
        from .util import parse_github_remote

        owner_repo = parse_github_remote(gitrepo.remote_url(repo))
    except gitrepo.GitError as exc:
        warnings.append(str(exc))

    base = "origin/main" if any(item["refname"] == "refs/remotes/origin/main" for item in branch_meta) else current
    branches = []
    names = []
    for item in branch_meta:
        refname = item["refname"]
        if refname.startswith("refs/heads/"):
            name = refname[len("refs/heads/") :]
            remote = 0
        else:
            name = refname[len("refs/remotes/") :]
            remote = 1
        names.append(name)
        ahead, behind = gitrepo.ahead_behind(repo, base, name) if name != base else (0, 0)
        branches.append(
            {
                "name": name,
                "sha": item["sha"],
                "subject": item["subject"],
                "committer_date": item["date"],
                "is_remote": remote,
                "ahead": ahead,
                "behind": behind,
            }
        )

    gh_token = resolve_token(token)
    if owner_repo:
        github = github_fetch(owner_repo[0], owner_repo[1], gh_token)
    elif gh_token:
        github = GitHubStatus("error", "origin is not a GitHub remote, so the token was not used.")
    else:
        github = GitHubStatus(
            "missing_token",
            "GITHUB_TOKEN is not set. Pull requests and CI checks are not loaded. Local git data is live.",
        )

    pull_branch_names = []
    for pull in github.pulls:
        local = f"origin/{pull['head_ref']}" if pull.get("head_ref") else ""
        if local and local in names:
            pull_branch_names.append(local)
        elif pull.get("head_sha"):
            pull_branch_names.append(pull["head_sha"])

    deep = _deep_refs(names, current, pull_branch_names)
    by_sha: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    ref_sha: dict[str, str] = {}
    for ref in deep:
        try:
            sha = gitrepo.rev_parse(repo, ref)
        except gitrepo.GitError as exc:
            warnings.append(f"{ref}: {exc}")
            continue
        ref_sha[ref] = sha
        if sha in by_sha:
            continue
        tests, artifacts, error = scan_commit(repo, sha)
        if error:
            warnings.append(f"{ref}: {error}")
        by_sha[sha] = (tests, artifacts)

    test_rows = []
    artifact_rows = []
    for ref, sha in ref_sha.items():
        tests, artifacts = by_sha.get(sha, ([], []))
        for row in tests:
            test_rows.append({"ref": ref, **row})
        for row in artifacts:
            artifact_rows.append({"ref": ref, **row})

    commits = []
    deep_set = set(ref_sha)
    seen_commit = set()
    for branch in branches:
        limit = 20 if branch["name"] in deep_set else 1
        if limit == 1:
            key = (branch["sha"], branch["name"])
            if key not in seen_commit:
                seen_commit.add(key)
                commits.append(
                    {
                        "sha": branch["sha"],
                        "branch": branch["name"],
                        "subject": branch["subject"],
                        "author": "",
                        "committed_at": branch["committer_date"],
                    }
                )
            continue
        for item in gitrepo.log(repo, branch["name"], limit):
            key = (item["sha"], branch["name"])
            if key in seen_commit:
                continue
            seen_commit.add(key)
            commits.append({"branch": branch["name"], **item})

    main_sha = ref_sha.get("origin/main") or ref_sha.get("main") or head
    store.replace_snapshot("branches", branches)
    store.replace_snapshot("commits", commits)
    store.replace_snapshot(
        "pulls",
        [
            {
                "number": pull["number"],
                "title": pull["title"],
                "state": pull["state"],
                "draft": pull["draft"],
                "merged": pull["merged"],
                "head_ref": pull["head_ref"],
                "head_sha": pull["head_sha"],
                "base_ref": pull["base_ref"],
                "html_url": pull["html_url"],
                "updated_at": pull["updated_at"],
            }
            for pull in github.pulls
            if pull.get("number") is not None
        ],
    )
    store.replace_snapshot("checks", github.checks)
    store.replace_snapshot("test_files", test_rows)
    store.replace_snapshot("artifacts", artifact_rows)

    tests_by_ref: dict[str, list[dict[str, Any]]] = {}
    for row in test_rows:
        tests_by_ref.setdefault(row["ref"], []).append(row)

    for ref, sha in ref_sha.items():
        if len(ref) == 40 and all(ch in "0123456789abcdef" for ch in ref):
            continue
        required = default_required(tests_by_ref.get(ref, []))
        contained = gitrepo.is_ancestor(repo, sha, main_sha) if main_sha else False
        _upsert_change(
            store,
            {
                "id": change_id_for_branch(ref),
                "title": ref,
                "ref": ref,
                "sha": sha,
                "pr_number": None,
                "source": "git",
                "required_source": "auto",
                "required_files": required,
                "contained_in_main": contained,
            },
        )

    for pull in github.pulls:
        head_ref = pull.get("head_ref") or ""
        local = f"origin/{head_ref}" if head_ref else ""
        ref = local if local in ref_sha else (pull.get("head_sha") or "")
        if not ref:
            continue
        sha = ref_sha.get(ref) or pull.get("head_sha") or ""
        required = default_required(tests_by_ref.get(ref, []))
        contained = bool(pull.get("merged")) or (bool(sha) and bool(main_sha) and gitrepo.is_ancestor(repo, sha, main_sha))
        _upsert_change(
            store,
            {
                "id": f"pr-{pull['number']}",
                "title": f"#{pull['number']} {pull.get('title') or ''}".strip(),
                "ref": ref,
                "sha": sha,
                "pr_number": pull["number"],
                "source": "github",
                "required_source": "auto",
                "required_files": required,
                "contained_in_main": contained,
            },
        )

    summary = {
        "fetched_at": utc_now(),
        "head": head,
        "branch": current,
        "worktree_dirty": dirty,
        "github": github.as_dict() | {"detail": github.detail, "state": github.state},
        "owner": owner_repo[0] if owner_repo else "",
        "repo": owner_repo[1] if owner_repo else "",
        "warnings": warnings,
        "counts": {
            "branches": len(branches),
            "commits": len(commits),
            "pulls": len(github.pulls),
            "checks": len(github.checks),
            "test_files": len(test_rows),
            "artifacts": len(artifact_rows),
            "deep_refs": len(ref_sha),
        },
    }
    store.meta_set("ingest", summary)
    store.meta_set("github", {"state": github.state, "detail": github.detail})
    store.event(
        "ingest",
        {
            "head": head,
            "github": github.state,
            "branches": len(branches),
            "test_files": len(test_rows),
            "artifacts": len(artifact_rows),
            "warnings": warnings[:12],
        },
    )
    return summary
