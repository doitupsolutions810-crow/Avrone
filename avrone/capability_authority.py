"""Fail-closed client adapter for capability-authority.

The authority service derives the caller from its workload identity. This client
never accepts model-supplied rights or caller identity. The broker public key
must be pinned out-of-band in AVRONE_PLANE_PUBLIC_KEY_B64.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_ACTION_RIGHTS = {
    "run_tests": {"type": "Execute", "resource": "task.run_tests"},
    "build_artifact": {"type": "Execute", "resource": "task.build_artifact"},
    "analyze_repository": {"type": "Execute", "resource": "task.analyze_repository"},
}


class AuthorityUnavailable(RuntimeError):
    """The authority did not return a verifiable decision."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _issue_sync(envelope: Mapping[str, Any], base_url: str, timeout: float) -> dict[str, Any]:
    action = envelope.get("requested_action")
    right = _ACTION_RIGHTS.get(action)
    if right is None:
        return {"decision": "DENY", "reason": "unsupported_action"}

    pinned = os.environ.get("AVRONE_PLANE_PUBLIC_KEY_B64", "")
    if not pinned:
        raise AuthorityUnavailable("broker public key pin is not configured")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pinned, validate=True))
    except Exception as exc:
        raise AuthorityUnavailable("broker public key pin is invalid") from exc

    body = json.dumps({
        "rights": [right],
        "ttl_seconds": min(300, max(1, int(envelope["resource_limits"]["timeout_seconds"]))),
        "constraints": {
            "rate_limit": {"count": 1, "window_seconds": 60},
            "task_id": str(envelope["task_id"]),
            "correlation_id": str(envelope["correlation_id"]),
        },
    }, separators=(",", ":"), allow_nan=False).encode()
    request = Request(
        base_url.rstrip("/") + "/v1/capabilities/issue",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise AuthorityUnavailable(f"authority HTTP status {response.status}")
            raw_payload = response.read(64 * 1024 + 1)
            if len(raw_payload) > 64 * 1024:
                raise AuthorityUnavailable("authority response exceeds size limit")
            payload = json.loads(raw_payload)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise AuthorityUnavailable("capability authority unavailable") from exc

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {"decision": "DENY", "reason": "authority_denied"}
    capability, signature = payload.get("capability"), payload.get("signature")
    if not isinstance(capability, dict) or not isinstance(signature, str):
        raise AuthorityUnavailable("authority response lacks capability or signature")
    try:
        public_key.verify(base64.b64decode(signature, validate=True), _canonical(capability))
    except Exception as exc:
        raise AuthorityUnavailable("capability signature verification failed") from exc

    expected_issuer = os.environ.get("AVRONE_PLANE_ISSUER", "spiffe://prod/broker")
    expected_domain = os.environ.get("AVRONE_PLANE_TRUST_DOMAIN", "prod")
    expected_audience = os.environ.get("AVRONE_PLANE_AUDIENCE", "")
    rights = capability.get("rights")
    expires = capability.get("expires_at")
    if (
        not expected_audience
        or capability.get("audience") != expected_audience
        or capability.get("issuer") != expected_issuer
        or capability.get("trust_domain") != expected_domain
        or not isinstance(expires, (int, float))
        or isinstance(expires, bool)
        or expires <= time.time()
        or not isinstance(rights, list)
        or right not in rights
    ):
        raise AuthorityUnavailable("signed capability does not match requested authority")
    return {
        "decision": "ALLOW",
        "policy_version": str(capability.get("policy_version", "unknown")),
        "capability": capability,
        "capability_signature": signature,
        "capability_id": capability.get("id"),
    }


async def authorize_with_capability_authority(
    envelope: Mapping[str, Any],
    *,
    base_url: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Issue and independently verify a narrowly scoped task capability."""
    url = base_url or os.environ.get("AVRONE_CAPABILITY_AUTHORITY_URL", "http://127.0.0.1:8090")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_issue_sync, envelope, url, timeout_seconds),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise AuthorityUnavailable("capability authority timed out") from exc
