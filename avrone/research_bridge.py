"""Revision-bound research bridge for Great Scale and AVRONE.

This module deliberately verifies public GitHub commit identity before a live council run.
It records what the system actually observed; it does not certify mathematical truth.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ResearchBridgeError(ValueError):
    """Input or upstream evidence could not be verified."""


def validate_source(repository: str, commit_sha: str) -> tuple[str, str]:
    repository = repository.strip()
    commit_sha = commit_sha.strip().lower()
    if not _REPO_RE.fullmatch(repository) or repository.startswith(".") or ".." in repository:
        raise ResearchBridgeError("repository must be owner/name")
    if not _SHA_RE.fullmatch(commit_sha):
        raise ResearchBridgeError("commit_sha must be a full 40-character Git commit SHA")
    return repository, commit_sha


async def verify_public_commit(repository: str, commit_sha: str) -> dict[str, str]:
    """Resolve a pinned public commit via GitHub's API; fail closed on any ambiguity."""
    repository, commit_sha = validate_source(repository, commit_sha)
    url = f"https://api.github.com/repos/{repository}/commits/{commit_sha}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ResearchBridgeError(f"GitHub commit lookup unavailable: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise ResearchBridgeError(f"GitHub commit lookup returned HTTP {response.status_code}; source not verified")
    try:
        data = response.json()
        resolved = str(data["sha"]).lower()
        html_url = str(data["html_url"])
    except (ValueError, KeyError, TypeError):
        raise ResearchBridgeError("GitHub returned malformed commit metadata") from None
    if resolved != commit_sha:
        raise ResearchBridgeError("GitHub returned a different commit SHA; refusing unpinned source")
    return {"repository": repository, "commit_sha": commit_sha, "commit_url": html_url,
            "verification": "github_public_commit_api_exact_sha"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write_receipt(data_dir: Path, *, run_id: str, source: dict[str, str],
                  claim: str, mode: str, rounds: int, result: dict[str, Any]) -> dict[str, Any]:
    """Persist an atomic receipt binding source SHA, live run id, and result digest."""
    if not _RUN_RE.fullmatch(run_id):
        raise ResearchBridgeError("invalid run id for receipt")
    answer = result.get("answer")
    if not isinstance(answer, str):
        answer = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    if len(answer) > 200_000:
        raise ResearchBridgeError("agent result exceeds the 200000-character receipt limit")
    payload = {
        "schema": "avrone.research-receipt.v1",
        "created_at_unix": time.time(),
        "run_id": run_id,
        "source": source,
        "claim": claim,
        "mode": mode,
        "rounds_requested": rounds,
        "result_digest_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "result": result,
        "verification_boundary": (
            "Source commit identity was checked against GitHub. The digest binds the recorded result; "
            "it is not a digital signature, mathematical proof, or proof that every tool action succeeded."
        ),
    }
    target_dir = Path(data_dir) / "research_receipts"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run_id}.json"
    encoded = _canonical_json(payload) + b"\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    payload["receipt_path"] = str(target)
    return payload


def read_receipt(data_dir: Path, run_id: str) -> dict[str, Any] | None:
    if not _RUN_RE.fullmatch(run_id):
        raise ResearchBridgeError("invalid run id")
    path = Path(data_dir) / "research_receipts" / f"{run_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ResearchBridgeError("receipt file could not be read or parsed") from None
    if not isinstance(value, dict) or value.get("schema") != "avrone.research-receipt.v1":
        raise ResearchBridgeError("receipt schema is invalid")
    return value
