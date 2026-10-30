"""Dependency-injected vertical-slice orchestrator.

This is the integration seam between AVRONE and the real policy/runner adapters.
It fails closed when authorization or execution raises. No permissive default
authorizer or host-shell runner is provided here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .execution_state import make_receipt, transition
from .task_contract import EnvelopeError, validate_envelope

Authorize = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
Runner = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def execute_vertical_slice(
    envelope: Mapping[str, Any],
    *,
    allowed_capabilities: set[str] | frozenset[str],
    authorize: Authorize,
    run_isolated: Runner,
    policy_timeout_seconds: float = 5.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate, authorize, invoke an injected isolated runner, and return evidence.

    The runner adapter is responsible for actual VM/container isolation, resource
    enforcement, cleanup, and output staging. This function does not claim to
    provide those properties itself.
    """
    state = "RECEIVED"
    validated = validate_envelope(envelope, allowed_capabilities=allowed_capabilities, now=now)
    state = transition(state, "VALIDATED")

    try:
        decision = await asyncio.wait_for(authorize(validated), timeout=policy_timeout_seconds)
    except asyncio.TimeoutError:
        return {"state": "UNKNOWN", "reason": "authorization_timeout", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    except Exception:
        return {"state": "UNKNOWN", "reason": "authorization_unavailable", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}

    if not isinstance(decision, Mapping) or decision.get("decision") not in {"ALLOW", "DENY"}:
        return {"state": "UNKNOWN", "reason": "invalid_authorization_response", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    if decision["decision"] == "DENY":
        state = transition(state, "DENIED")
        return {"state": state, "reason": "policy_denied", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"], "policy_version": str(decision.get("policy_version", "unknown"))}

    state = transition(state, "AUTHORIZED")
    state = transition(state, "DISPATCHED")
    try:
        result = await asyncio.wait_for(
            run_isolated(validated),
            timeout=validated["resource_limits"]["timeout_seconds"],
        )
    except asyncio.TimeoutError:
        return {"state": "TIMED_OUT", "reason": "runner_timeout", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    except Exception:
        return {"state": "WORKER_FAILED", "reason": "runner_failed", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}

    state = transition(state, "RUNNING")
    if not isinstance(result, Mapping) or result.get("status") != "completed":
        return {"state": "WORKER_FAILED", "reason": "invalid_runner_result", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    digests = result.get("artifact_digests", [])
    if not isinstance(digests, list):
        return {"state": "VERIFICATION_FAILED", "reason": "invalid_artifact_manifest", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    state = transition(state, "COMPLETED")
    # Runner output is not trusted to assert verification. Independently check
    # only the manifest's digest syntax here; artifact bytes require a verifier.
    try:
        receipt = make_receipt(
            correlation_id=validated["correlation_id"],
            task_id=validated["task_id"],
            source_repository=validated["source"]["repository"],
            source_commit=validated["source"]["commit_sha"],
            authorization="ALLOW",
            policy_version=str(decision.get("policy_version", "unknown")),
            worker_id=str(result.get("worker_id", "")) or None,
            outcome="VERIFIED",
            artifact_digests=digests,
            verifier_version="manifest-syntax-only-v1",
        )
    except (TypeError, ValueError):
        return {"state": "VERIFICATION_FAILED", "reason": "receipt_validation_failed", "correlation_id": validated["correlation_id"], "task_id": validated["task_id"]}
    state = transition(state, "VERIFIED")
    return {"state": state, "receipt": receipt}
