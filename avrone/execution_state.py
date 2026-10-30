"""Fail-closed execution state transitions for the vertical-slice contract."""
from __future__ import annotations

from typing import Any, Mapping

from .task_contract import canonical_digest

TERMINAL = frozenset({
    "VERIFIED", "REJECTED", "DENIED", "TIMED_OUT", "CANCELLED",
    "WORKER_FAILED", "VERIFICATION_FAILED", "UNKNOWN",
})
_TRANSITIONS = {
    "RECEIVED": frozenset({"VALIDATED", "REJECTED"}),
    "VALIDATED": frozenset({"AUTHORIZED", "DENIED", "REJECTED"}),
    "AUTHORIZED": frozenset({"DISPATCHED", "UNKNOWN"}),
    "DISPATCHED": frozenset({"RUNNING", "UNKNOWN", "CANCELLED"}),
    "RUNNING": frozenset({"COMPLETED", "TIMED_OUT", "CANCELLED", "WORKER_FAILED", "UNKNOWN"}),
    "COMPLETED": frozenset({"VERIFIED", "VERIFICATION_FAILED"}),
}


class StateTransitionError(ValueError):
    """Raised for an illegal or ambiguous execution-state transition."""


def transition(current: str, target: str) -> str:
    """Validate a single state transition; terminal states cannot be reopened."""
    if current in TERMINAL or target not in _TRANSITIONS.get(current, frozenset()):
        raise StateTransitionError(f"illegal execution transition: {current!r} -> {target!r}")
    return target


def make_receipt(
    *,
    correlation_id: str,
    task_id: str,
    source_repository: str,
    source_commit: str,
    authorization: str,
    policy_version: str,
    worker_id: str | None,
    outcome: str,
    artifact_digests: list[str],
    verifier_version: str,
) -> dict[str, Any]:
    """Create a deterministic receipt payload and its integrity digest.

    This digest detects accidental or post-creation mutation; it is not a
    digital signature and does not prove who produced the receipt.
    """
    if authorization not in {"ALLOW", "DENY", "NOT_EVALUATED"}:
        raise ValueError("invalid authorization outcome")
    if outcome not in TERMINAL:
        raise ValueError("receipt outcome must be terminal")
    if not isinstance(artifact_digests, list) or any(
        not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
        for d in artifact_digests
    ):
        raise ValueError("artifact_digests must contain lowercase SHA-256 digests")
    if outcome == "VERIFIED" and (authorization != "ALLOW" or not worker_id):
        raise ValueError("VERIFIED requires ALLOW authorization and a worker identity")
    if authorization == "DENY" and outcome != "DENIED":
        raise ValueError("denied tasks must have DENIED outcome")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "correlation_id": correlation_id,
        "task_id": task_id,
        "source": {"repository": source_repository, "commit_sha": source_commit},
        "authorization": authorization,
        "policy_version": policy_version,
        "worker_id": worker_id,
        "outcome": outcome,
        "artifact_digests": list(artifact_digests),
        "verifier_version": verifier_version,
    }
    payload["receipt_sha256"] = canonical_digest(payload)
    return payload


def verify_receipt(receipt: Mapping[str, Any]) -> bool:
    """Check receipt payload integrity. This is not signature authentication."""
    if not isinstance(receipt, Mapping) or "receipt_sha256" not in receipt:
        return False
    supplied = receipt.get("receipt_sha256")
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    try:
        return isinstance(supplied, str) and canonical_digest(payload) == supplied
    except (TypeError, ValueError):
        return False
