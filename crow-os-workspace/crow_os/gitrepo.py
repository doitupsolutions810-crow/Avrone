"""Read-only git access plus detached worktrees.

The main working copy is never checked out or reset.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    return env


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=git_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitError(detail or f"git {' '.join(args)} failed")
    return completed


def repo_root_from(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def remote_url(repo: Path) -> str:
    completed = git(repo, "remote", "get-url", "origin", check=False)
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def current_branch(repo: Path) -> str:
    completed = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    name = (completed.stdout or "").strip()
    return name or "HEAD"


def rev_parse(repo: Path, ref: str) -> str:
    if any(token in ref for token in (" ", "\n", "\t", "..")):
        raise GitError("ref is not a single revision")
    completed = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", check=False)
    if completed.returncode != 0:
        raise GitError((completed.stderr or f"unknown ref {ref}").strip())
    return completed.stdout.strip()


def worktree_dirty(repo: Path) -> bool:
    completed = git(repo, "status", "--porcelain", check=False)
    return bool((completed.stdout or "").strip())


def for_each_ref(repo: Path) -> list[dict[str, str]]:
    completed = git(
        repo,
        "for-each-ref",
        "--format=%(refname)\t%(objectname)\t%(committerdate:iso-strict)\t%(subject)",
        "refs/heads",
        "refs/remotes",
    )
    rows = []
    for line in (completed.stdout or "").splitlines():
        refname, sha, date, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        if refname.endswith("/HEAD") or refname == "refs/remotes/origin/HEAD":
            continue
        rows.append({"refname": refname, "sha": sha, "date": date, "subject": subject})
    return rows


def ahead_behind(repo: Path, base: str, ref: str) -> tuple[int | None, int | None]:
    completed = git(repo, "rev-list", "--left-right", "--count", f"{base}...{ref}", check=False)
    if completed.returncode != 0:
        return None, None
    parts = (completed.stdout or "").split()
    if len(parts) != 2:
        return None, None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


def log(repo: Path, ref: str, limit: int = 20) -> list[dict[str, str]]:
    completed = git(
        repo,
        "log",
        f"-n{limit}",
        "--date=iso-strict",
        "--pretty=format:%H\t%an\t%ad\t%s",
        ref,
        check=False,
    )
    if completed.returncode != 0:
        return []
    rows = []
    for line in (completed.stdout or "").splitlines():
        sha, author, date, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        if sha:
            rows.append({"sha": sha, "author": author, "committed_at": date, "subject": subject})
    return rows


def ls_tree(repo: Path, ref: str) -> list[dict[str, object]]:
    completed = git(repo, "ls-tree", "-r", "-l", "--full-tree", ref, check=False)
    if completed.returncode != 0:
        return []
    rows = []
    for line in (completed.stdout or "").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or not path:
            continue
        size_raw = parts[3]
        try:
            size = int(size_raw) if size_raw != "-" else 0
        except ValueError:
            size = 0
        rows.append({"mode": parts[0], "type": parts[1], "blob": parts[2], "size": size, "path": path})
    return rows


def cat_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    try:
        completed = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=str(repo),
            input=("\n".join(shas) + "\n").encode(),
            capture_output=True,
            env=git_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    if completed.returncode != 0:
        raise GitError((completed.stderr or b"git cat-file failed").decode(errors="replace").strip())
    data = completed.stdout
    pos = 0
    found: dict[str, bytes] = {}
    for sha in shas:
        newline = data.find(b"\n", pos)
        if newline < 0:
            found[sha] = b""
            break
        header = data[pos:newline].decode(errors="replace")
        pos = newline + 1
        parts = header.split()
        if len(parts) >= 3 and parts[1] in {"blob", "commit", "tree"}:
            size = int(parts[2])
            blob = data[pos : pos + size]
            pos += size
            if data[pos : pos + 1] == b"\n":
                pos += 1
            found[sha] = blob
        else:
            found[sha] = b""
    return found


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    completed = git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return completed.returncode == 0


def add_worktree_branch(repo: Path, path: Path, sha: str, branch: str) -> None:
    """Create a new branch and worktree. Does not move the main checkout."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("/", ".")) or ".." in branch.split("/"):
        raise GitError("branch name is not allowed")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        remove_worktree(repo, path)
    git(repo, "worktree", "add", "-b", branch, str(path), sha)


def add_worktree(repo: Path, path: Path, sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        remove_worktree(repo, path)
    git(repo, "worktree", "add", "--detach", "--force", str(path), sha)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)
