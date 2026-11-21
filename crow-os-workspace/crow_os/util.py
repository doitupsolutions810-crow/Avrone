"""Small shared helpers. No I/O."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def clip(text: str, limit: int = 200_000) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    note = f"\n\n[stored preview truncated at {limit} characters; the on-disk log and its SHA-256 are complete]\n"
    return text[:limit] + note, True


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) from a GitHub remote URL. Credentials are ignored."""
    if not url:
        return None
    cleaned = re.sub(r"https://[^@/]+@", "https://", url.strip())
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", cleaned)
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def change_id_for_branch(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return f"branch-{safe}"[:140]


def is_relative_test_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return path.endswith(".py")


def classify_suite(path: str) -> str:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if lowered.startswith("avrone-a12lr/") or "/avrone-a12lr/" in lowered:
        return "daemon-edge"
    if "edge" in name:
        return "edge"
    if lowered.startswith("avrone-squad/"):
        return "squad"
    if lowered.startswith("tests/") or "/tests/" in lowered:
        return "core"
    return "other"


def artifact_kind(path: str) -> str | None:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if name in {"package-lock.json", "package.json"} or lowered.endswith((".py", ".sh")):
        return None
    if lowered.endswith(".jsonl"):
        return "jsonl"
    if "evidence" in name:
        return "evidence"
    if name == "manifest.json":
        return "manifest"
    if lowered.endswith(".json"):
        return "json"
    return None


def components_in_source(text: str) -> list[str]:
    match = re.search(r"COMPONENTS\s*=\s*\[(.*?)\]", text, re.S)
    if not match:
        return []
    found = re.findall(r"""['"]([A-Za-z0-9_.-]+)['"]""", match.group(1))
    seen: list[str] = []
    for item in found:
        if item not in seen:
            seen.append(item)
    return seen


def json_ready(value: Any) -> Any:
    return value
