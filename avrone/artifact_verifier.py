"""Independent verifier for artifacts staged by a worker.

The worker's claimed digest is treated only as a declaration. This verifier
opens staged bytes itself, enforces a confined root and output budget, and
recomputes SHA-256 before AVRONE can mark a task VERIFIED.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def verify_staged_artifacts(
    task: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: str | Path | None = None,
) -> bool:
    raw_root = root if root is not None else os.environ.get("AVRONE_ARTIFACT_ROOT")
    if raw_root is None or str(raw_root).strip() == "":
        return False
    configured_root = Path(raw_root).expanduser()
    try:
        base = configured_root.resolve(strict=True)
    except OSError:
        return False
    if not base.is_dir():
        return False

    digests = result.get("artifact_digests")
    artifacts = result.get("artifacts")
    if not isinstance(digests, list) or not isinstance(artifacts, list):
        return False
    if len(digests) != len(artifacts) or len(set(digests)) != len(digests):
        return False
    if any(not isinstance(d, str) or not _DIGEST.fullmatch(d) for d in digests):
        return False

    limits = task.get("resource_limits")
    if not isinstance(limits, Mapping):
        return False
    budget = limits.get("output_bytes")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        return False

    observed: list[str] = []
    total = 0
    for item in artifacts:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            return False
        rel = item.get("path")
        declared = item.get("sha256")
        if not isinstance(rel, str) or not rel or not isinstance(declared, str):
            return False
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or not _DIGEST.fullmatch(declared):
            return False

        candidate = base / rel_path
        # Reject symlinks in every path component, even when they resolve back
        # inside the staging root; the worker must provide ordinary staged files.
        cursor = base
        for part in rel_path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(base)
            if not resolved.is_file():
                return False
            size = resolved.stat().st_size
            total += size
            if total > budget:
                return False
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except (OSError, ValueError):
            return False
        if digest != declared:
            return False
        observed.append(digest)

    return observed == digests


async def verify_staged_artifacts_async(
    task: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: str | Path | None = None,
) -> bool:
    return await asyncio.to_thread(verify_staged_artifacts, task, result, root=root)
