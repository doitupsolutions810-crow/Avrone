"""Revision-bound research bridge for Great Scale and AVRONE.

This module deliberately verifies public GitHub commit identity before a live council run.
It records what the system actually observed; it does not certify mathematical truth.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import stat
import json
import os
import re
import tempfile
import time
import base64
from urllib.parse import quote
from pathlib import Path
from typing import Any

import httpx

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ResearchBridgeError(ValueError):
    """Input or upstream evidence could not be verified."""


def validate_source(repository: str, commit_sha: str) -> tuple[str, str]:
    if not isinstance(repository, str) or not isinstance(commit_sha, str):
        raise ResearchBridgeError("repository and commit_sha must be strings")
    repository = repository.strip()
    commit_sha = commit_sha.strip().lower()
    if not _REPO_RE.fullmatch(repository) or repository.startswith(".") or ".." in repository:
        raise ResearchBridgeError("repository must be owner/name")
    if not _SHA_RE.fullmatch(commit_sha):
        raise ResearchBridgeError("commit_sha must be a full 40-character Git commit SHA")
    return repository, commit_sha


async def verify_github_commit(repository: str, commit_sha: str) -> dict[str, Any]:
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
        commit_message = str(data.get("commit", {}).get("message", ""))[:1000]
        tree_sha = str(data.get("commit", {}).get("tree", {}).get("sha", ""))
        parents = [str(p.get("sha", "")) for p in data.get("parents", [])[:8]]
        changed_files = [
            {"path": str(f.get("filename", "")), "status": str(f.get("status", "")),
             "additions": int(f.get("additions", 0)), "deletions": int(f.get("deletions", 0))}
            for f in data.get("files", [])[:50]
        ]
    except (ValueError, KeyError, TypeError):
        raise ResearchBridgeError("GitHub returned malformed commit metadata") from None
    if resolved != commit_sha:
        raise ResearchBridgeError("GitHub returned a different commit SHA; refusing unpinned source")
    return {"repository": repository, "commit_sha": commit_sha, "commit_url": html_url,
            "commit_message": commit_message, "tree_sha": tree_sha, "parent_shas": parents,
            "changed_files": changed_files,
            "verification": "github_commit_api_exact_sha"}


DEFAULT_SOURCE_PATHS = (
    "public/atlas.html",
    "research/ENDOMORPHISM_DEBATE_ATLAS.md",
    "test_atlas_contract.cjs",
    "package.json",
)


async def fetch_source_files(repository: str, commit_sha: str, paths: list[str]) -> list[dict[str, Any]]:
    """Fetch bounded text files at the pinned SHA; never fetch arbitrary hosts or execute code."""
    repository, commit_sha = validate_source(repository, commit_sha)
    if not paths:
        paths = list(DEFAULT_SOURCE_PATHS)
    if len(paths) > 5:
        raise ResearchBridgeError("source_paths accepts at most 5 files per run")
    clean_paths: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            raise ResearchBridgeError("source paths must be strings")
        path = raw.strip()
        if path.startswith("/") or path.endswith("/"):
            raise ResearchBridgeError("source paths must be relative file paths")
        parts = path.split("/")
        if (not path or len(path) > 240 or any(p in ("", ".", "..") for p in parts)
                or not re.fullmatch(r"[A-Za-z0-9_.@/-]+", path)):
            raise ResearchBridgeError("source paths must be repository-relative paths without traversal")
        if path not in clean_paths:
            clean_paths.append(path)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    results: list[dict[str, Any]] = []
    total_chars = 0
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        for path in clean_paths:
            url = f"https://api.github.com/repos/{repository}/contents/{quote(path, safe='/')}?ref={commit_sha}"
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise ResearchBridgeError(f"GitHub source fetch unavailable: {type(exc).__name__}") from None
            if response.status_code == 404:
                results.append({"path": path, "status": "not_found_at_pinned_sha"})
                continue
            if response.status_code != 200:
                raise ResearchBridgeError(f"GitHub source fetch for {path} returned HTTP {response.status_code}")
            try:
                obj = response.json()
                if obj.get("type") != "file" or obj.get("encoding") != "base64":
                    raise ValueError("not a base64 file")
                encoded_content = obj["content"]
                if not isinstance(encoded_content, str) or len(encoded_content) > 1_400_000:
                    raise ValueError("encoded source exceeds limit")
                raw_bytes = base64.b64decode(encoded_content, validate=True)
                if len(raw_bytes) > 1_000_000:
                    raise ValueError("source file exceeds 1 MB")
                text_content = raw_bytes.decode("utf-8")
                blob_sha = str(obj["sha"]).lower()
                expected_blob_sha = hashlib.sha1(b"blob " + str(len(raw_bytes)).encode("ascii") + b"\0" + raw_bytes).hexdigest()
                if not re.fullmatch(r"[0-9a-f]{40}", blob_sha) or not hmac.compare_digest(blob_sha, expected_blob_sha):
                    raise ValueError("Git blob SHA-1 mismatch")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                results.append({"path": path, "status": "non_text_or_unreadable"})
                continue
            room = max(0, 30000 - total_chars)
            excerpt = text_content[:min(12000, room)]
            total_chars += len(excerpt)
            results.append({"path": path, "status": "fetched", "blob_sha": blob_sha,
                            "bytes": len(raw_bytes), "truncated": len(excerpt) < len(text_content),
                            "content": excerpt})
            if total_chars >= 30000:
                break
    if not any(item.get("status") == "fetched" for item in results):
        raise ResearchBridgeError("none of the requested source files could be fetched at the pinned SHA")
    return results


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ResearchBridgeError("value is not valid canonical JSON") from None


def write_receipt(data_dir: Path, *, run_id: str, source: dict[str, str],
                  claim: str, mode: str, rounds: int, result: dict[str, Any]) -> dict[str, Any]:
    """Persist an atomic receipt binding source SHA, live run id, and result digest."""
    if not isinstance(run_id, str) or not _RUN_RE.fullmatch(run_id):
        raise ResearchBridgeError("invalid run id for receipt")
    if not isinstance(source, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in source.items()):
        raise ResearchBridgeError("receipt source must be a string-to-string mapping")
    if not isinstance(claim, str) or not claim or len(claim) > 4000:
        raise ResearchBridgeError("receipt claim must contain 1-4000 characters")
    if not isinstance(mode, str) or not mode or len(mode) > 64:
        raise ResearchBridgeError("receipt mode must be a non-empty string of at most 64 characters")
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 3:
        raise ResearchBridgeError("receipt rounds must be an integer from 1 to 3")
    if not isinstance(result, dict):
        raise ResearchBridgeError("receipt result must be an object")
    answer = result.get("answer")
    if not isinstance(answer, str):
        answer = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    if len(answer) > 200_000:
        raise ResearchBridgeError("agent result exceeds the 200000-character receipt limit")
    result_bytes = _canonical_json(result)
    if len(result_bytes) > 2_000_000:
        raise ResearchBridgeError("complete result envelope exceeds the 2 MB receipt limit")
    result_digest = hashlib.sha256(result_bytes).hexdigest()
    binding = {
        "schema": "avrone.research-receipt.v1",
        "run_id": run_id,
        "source": source,
        "claim": claim,
        "mode": mode,
        "rounds_requested": rounds,
        "result_digest_sha256": result_digest,
    }
    evidence_digest = hashlib.sha256(_canonical_json(binding)).hexdigest()
    payload = {
        **binding,
        "created_at_unix": time.time(),
        "result_digest_sha256": result_digest,
        "evidence_digest_sha256": evidence_digest,
        "result": result,
        "verification_boundary": (
            "The evidence digest binds source identity, claim, mode, round count, and result digest. "
            "It is not a digital signature, mathematical proof, or proof that every tool action succeeded."
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
    """Read and cryptographically revalidate a bounded receipt without following symlinks."""
    if not isinstance(run_id, str) or not _RUN_RE.fullmatch(run_id):
        raise ResearchBridgeError("invalid run id")
    directory = Path(data_dir) / "research_receipts"
    dir_fd = None
    file_fd = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            file_fd = os.open(f"{run_id}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_100_000:
            raise ResearchBridgeError("receipt is not a regular file or exceeds the 2.1 MB read limit")
        chunks = []
        remaining = 2_100_001
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 2_100_000:
            raise ResearchBridgeError("receipt exceeds the 2.1 MB read limit")
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError:
        return None
    except ResearchBridgeError:
        raise
    except (OSError, ValueError, UnicodeDecodeError):
        raise ResearchBridgeError("receipt file could not be safely read or parsed") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if dir_fd is not None:
            os.close(dir_fd)

    if not isinstance(value, dict) or value.get("schema") != "avrone.research-receipt.v1":
        raise ResearchBridgeError("receipt schema is invalid")
    binding_keys = ("schema", "run_id", "source", "claim", "mode", "rounds_requested", "result_digest_sha256")
    if value.get("run_id") != run_id or any(key not in value for key in binding_keys):
        raise ResearchBridgeError("receipt binding is incomplete or mismatched")
    if (not isinstance(value.get("source"), dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in value["source"].items())
            or not isinstance(value.get("claim"), str) or not value["claim"]
            or not isinstance(value.get("mode"), str) or not value["mode"]
            or isinstance(value.get("rounds_requested"), bool)
            or not isinstance(value.get("rounds_requested"), int)
            or not 1 <= value["rounds_requested"] <= 3):
        raise ResearchBridgeError("receipt binding fields have invalid types or values")
    result = value.get("result")
    if not isinstance(result, dict):
        raise ResearchBridgeError("receipt result must be an object")
    try:
        result_digest = hashlib.sha256(_canonical_json(result)).hexdigest()
        if not hmac.compare_digest(str(value.get("result_digest_sha256", "")), result_digest):
            raise ResearchBridgeError("receipt result digest verification failed")
        binding = {key: value[key] for key in binding_keys}
        evidence_digest = hashlib.sha256(_canonical_json(binding)).hexdigest()
        if not hmac.compare_digest(str(value.get("evidence_digest_sha256", "")), evidence_digest):
            raise ResearchBridgeError("receipt evidence digest verification failed")
        created = float(value["created_at_unix"])
        if not math.isfinite(created):
            raise ValueError("invalid timestamp")
    except ResearchBridgeError:
        raise
    except (KeyError, TypeError, ValueError):
        raise ResearchBridgeError("receipt integrity metadata is invalid") from None
    return value
