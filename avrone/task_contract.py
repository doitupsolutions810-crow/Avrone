"""Strict, dependency-free task-envelope validation for cross-service execution.

This module validates requests; it does not authorize or execute them. The
control plane and isolated runner remain separate trust boundaries.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_INPUT_ARTIFACTS = 64
MAX_CAPABILITIES = 32
MAX_TIMEOUT_SECONDS = 3600
MAX_MEMORY_MB = 8192
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"run_tests", "build_artifact", "analyze_repository"})


class EnvelopeError(ValueError):
    """Raised when a task envelope is malformed or exceeds a hard limit."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise EnvelopeError(f"{field} must be a valid identifier")
    return value


def _positive_int(value: Any, field: str, maximum: int) -> int:
    # bool is an int subclass, but is never a valid resource limit.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise EnvelopeError(f"{field} must be an integer between 1 and {maximum}")
    return value


def validate_envelope(
    envelope: Mapping[str, Any],
    *,
    allowed_capabilities: set[str] | frozenset[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a normalized copy, rejecting malformed or over-privileged input.

    Identity fields are syntax-checked only. Callers must populate them from
    authenticated server context and obtain an independent control-plane decision.
    """
    if not isinstance(envelope, Mapping):
        raise EnvelopeError("envelope must be an object")
    try:
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnvelopeError("envelope must contain finite JSON values only") from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError("envelope exceeds maximum encoded size")

    required = {
        "schema_version", "correlation_id", "task_id", "source",
        "requested_action", "tenant_id", "principal_id",
        "requested_capabilities", "resource_limits",
        "input_artifact_digests", "deadline",
    }
    if set(envelope) != required:
        missing, extra = sorted(required - set(envelope)), sorted(set(envelope) - required)
        raise EnvelopeError(f"invalid envelope keys; missing={missing}, extra={extra}")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != SCHEMA_VERSION:
        raise EnvelopeError("unsupported schema_version")

    out = dict(envelope)
    for key in ("correlation_id", "task_id", "tenant_id", "principal_id"):
        out[key] = _identifier(envelope[key], key)

    source = envelope["source"]
    if not isinstance(source, Mapping) or set(source) != {"repository", "commit_sha"}:
        raise EnvelopeError("source must contain repository and commit_sha only")
    if not isinstance(source["repository"], str) or not _REPO.fullmatch(source["repository"]):
        raise EnvelopeError("source.repository must be owner/repository")
    if not isinstance(source["commit_sha"], str) or not _SHA.fullmatch(source["commit_sha"]):
        raise EnvelopeError("source.commit_sha must be a full lowercase commit SHA")
    out["source"] = dict(source)

    action = envelope["requested_action"]
    if not isinstance(action, str) or action not in _ACTIONS:
        raise EnvelopeError("requested_action is not an allowed enumerated action")
    out["requested_action"] = action

    caps = envelope["requested_capabilities"]
    if not isinstance(caps, list) or len(caps) > MAX_CAPABILITIES:
        raise EnvelopeError("requested_capabilities must be a bounded list")
    if any(not isinstance(c, str) or not _ID.fullmatch(c) for c in caps):
        raise EnvelopeError("each capability must be a valid identifier")
    if len(set(caps)) != len(caps):
        raise EnvelopeError("duplicate capabilities are not allowed")
    allowed = set(allowed_capabilities)
    if any(c not in allowed for c in caps):
        raise EnvelopeError("requested capabilities exceed configured allowlist")
    out["requested_capabilities"] = sorted(caps)

    limits = envelope["resource_limits"]
    if not isinstance(limits, Mapping) or set(limits) != {"timeout_seconds", "memory_mb", "output_bytes"}:
        raise EnvelopeError("resource_limits has an invalid shape")
    out["resource_limits"] = {
        "timeout_seconds": _positive_int(limits["timeout_seconds"], "timeout_seconds", MAX_TIMEOUT_SECONDS),
        "memory_mb": _positive_int(limits["memory_mb"], "memory_mb", MAX_MEMORY_MB),
        "output_bytes": _positive_int(limits["output_bytes"], "output_bytes", MAX_OUTPUT_BYTES),
    }

    artifacts = envelope["input_artifact_digests"]
    if not isinstance(artifacts, list) or len(artifacts) > MAX_INPUT_ARTIFACTS:
        raise EnvelopeError("input_artifact_digests must be a bounded list")
    if any(not isinstance(d, str) or not _DIGEST.fullmatch(d) for d in artifacts):
        raise EnvelopeError("artifact digests must be lowercase SHA-256 hex")
    if len(set(artifacts)) != len(artifacts):
        raise EnvelopeError("duplicate artifact digests are not allowed")
    out["input_artifact_digests"] = list(artifacts)

    deadline = envelope["deadline"]
    if not isinstance(deadline, str):
        raise EnvelopeError("deadline must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError("deadline must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnvelopeError("deadline must include a timezone")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise EnvelopeError("now must be timezone-aware")
    remaining = (parsed.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise EnvelopeError("deadline must be in the future")
    if remaining > MAX_TIMEOUT_SECONDS:
        raise EnvelopeError("deadline exceeds the maximum task duration")
    out["deadline"] = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return out


def canonical_digest(value: Mapping[str, Any]) -> str:
    """SHA-256 digest of a JSON-compatible mapping with deterministic encoding."""
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnvelopeError("value must be finite JSON data") from exc
    return hashlib.sha256(payload).hexdigest()


def verify_artifact_bytes(content: bytes, expected_sha256: str) -> bool:
    """Verify artifact bytes against a lowercase SHA-256 digest."""
    if not isinstance(content, bytes) or not isinstance(expected_sha256, str):
        return False
    if not _DIGEST.fullmatch(expected_sha256):
        return False
    return hashlib.sha256(content).hexdigest() == expected_sha256
