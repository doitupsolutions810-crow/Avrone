from __future__ import annotations

import base64
import hashlib
import math
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ACTION = {"type": "Execute", "resource": "swarm.jobs", "method": "POST"}
ENVELOPE_SCHEMA = "avrone.message-envelope/v1"
RECEIPT_FIELDS = {
    "message_id", "correlation_id", "capability_id", "issuer", "executor",
    "action", "resource", "method", "outcome", "timestamp", "payload_digest",
    "previous_receipt_digest", "signature",
}


def _jcs(value: Any) -> bytes:
    return rfc8785.dumps(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_jcs(value)).hexdigest()


def _b64(value: str, size: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("signature/key must be canonical base64") from exc
    if len(raw) != size or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("signature/key has invalid length or encoding")
    return raw


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SwarmError(RuntimeError):
    pass


class SwarmRejected(SwarmError):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"Swarm rejected request ({status_code}): {detail}")
        self.status_code, self.detail = status_code, detail


class ReplayConflict(SwarmRejected):
    pass


class UnknownOutcome(SwarmError):
    """Retry this exact signed request only. Never mint a new id after a timeout."""
    def __init__(self, message: str, request: dict[str, Any]):
        super().__init__(message)
        self.request = request
        self.message_id = request.get("envelope", {}).get("message_id")
        self.correlation_id = request.get("envelope", {}).get("correlation_id")


class AuthorityRejected(SwarmError):
    def __init__(self, detail: Any):
        super().__init__(f"Capability Authority rejected delegation: {detail}")
        self.detail = detail


class DelegationUnknownOutcome(SwarmError):
    """Retry the same delegation correlation; the Authority may already have persisted it."""
    def __init__(self, message: str, args: dict[str, Any]):
        super().__init__(message)
        self.args_for_retry = args
        self.correlation_id = args["correlation_id"]


class CapabilityAuthorityClient:
    def __init__(
        self, *, endpoint: str, requester: str, authority_issuer: str,
        requester_private_key_pem: bytes | str, authority_public_key_b64: str,
        http_client: httpx.Client | None = None, allow_insecure_http: bool = False,
    ):
        parsed = httpx.URL(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("Capability Authority endpoint must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.host not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure_http:
            raise ValueError("non-loopback Capability Authority endpoints require HTTPS")
        self.endpoint = endpoint.rstrip("/") + "/v1/capabilities/delegate"
        self.requester, self.authority_issuer = requester, authority_issuer
        private_bytes = requester_private_key_pem.encode() if isinstance(requester_private_key_pem, str) else requester_private_key_pem
        self._requester_key = serialization.load_pem_private_key(private_bytes, password=None)
        if not isinstance(self._requester_key, Ed25519PrivateKey):
            raise ValueError("requester assertion signing key must be Ed25519")
        self._authority_key = Ed25519PublicKey.from_public_bytes(_b64(authority_public_key_b64, 32))
        self._http = http_client or httpx.Client()

    @classmethod
    def from_env(cls, http_client: httpx.Client | None = None) -> "CapabilityAuthorityClient":
        names = {"AVRONE_CA_URL": os.getenv("AVRONE_CA_URL", ""),
                 "AVRONE_CA_REQUESTER_ID": os.getenv("AVRONE_CA_REQUESTER_ID", ""),
                 "AVRONE_CA_ISSUER": os.getenv("AVRONE_CA_ISSUER", ""),
                 "AVRONE_CA_REQUESTER_KEY_FILE": os.getenv("AVRONE_CA_REQUESTER_KEY_FILE", ""),
                 "AVRONE_CA_PUBLIC_KEY_B64": os.getenv("AVRONE_CA_PUBLIC_KEY_B64", "")}
        missing = [k for k,v in names.items() if not v]
        if missing: raise RuntimeError("Capability Authority is not configured: " + ", ".join(missing))
        key = Path(names["AVRONE_CA_REQUESTER_KEY_FILE"])
        fd = os.open(key, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not __import__("stat").S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_mode & 0o077:
                raise RuntimeError("requester key must be a private regular file (0600) under 16 KiB")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                private_key = stream.read(16385)
        finally:
            os.close(fd)
        if len(private_key) > 16384:
            raise RuntimeError("requester key exceeds 16 KiB")
        return cls(endpoint=names["AVRONE_CA_URL"], requester=names["AVRONE_CA_REQUESTER_ID"],
                   authority_issuer=names["AVRONE_CA_ISSUER"], requester_private_key_pem=private_key,
                   authority_public_key_b64=names["AVRONE_CA_PUBLIC_KEY_B64"], http_client=http_client,
                   allow_insecure_http=os.getenv("AVRONE_CA_ALLOW_HTTP", "0") == "1")

    def _assertion(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("assertion clock must be timezone-aware")
        unsigned = {
            "schema": "capability-authority.delegation-assertion/v1",
            "issuer": self.requester,
            "subject": self.requester,
            "audience": self.authority_issuer,
            "jti": str(uuid4()),
            "issued_at": _iso(current),
            "expires_at": _iso(current + timedelta(seconds=30)),
        }
        return {**unsigned, "signature": base64.b64encode(self._requester_key.sign(_jcs(unsigned))).decode("ascii")}

    def _verify_parent(
        self, parent: dict[str, Any], signature: str,
        rights: list[dict[str, str]], now: datetime, *, allow_expired: bool = False,
    ) -> None:
        if not isinstance(parent, dict) or parent.get("issuer") != self.authority_issuer or parent.get("audience") != self.requester:
            raise ValueError("parent capability issuer/audience mismatch")
        if not isinstance(parent.get("id"), str) or not parent["id"].strip():
            raise ValueError("parent capability id is required")
        issued, expires = parent.get("issued_at"), parent.get("expires_at")
        if isinstance(issued, bool) or isinstance(expires, bool) or not isinstance(issued, (int, float)) or not isinstance(expires, (int, float)):
            raise ValueError("parent capability timestamps must be numbers")
        if not math.isfinite(issued) or not math.isfinite(expires) or issued > now.timestamp() + 30 or expires <= issued:
            raise ValueError("parent capability timestamps are invalid")
        if expires <= now.timestamp() and not allow_expired:
            raise ValueError("parent capability is expired")
        parent_rights = parent.get("rights")
        if not isinstance(parent_rights, list):
            raise ValueError("parent capability rights are invalid")
        allowed = set()
        for item in parent_rights:
            if not isinstance(item, dict):
                continue
            parts = (item.get("type"), item.get("resource"), item.get("method"))
            if all(isinstance(part, str) and part.strip() for part in parts):
                allowed.add(parts)
        for right in rights:
            if not isinstance(right, dict) or set(right) != {"type", "resource", "method"}:
                raise ValueError("delegated right schema is invalid")
            parts = (right.get("type"), right.get("resource"), right.get("method"))
            if not all(isinstance(part, str) and part.strip() for part in parts):
                raise ValueError("delegated right fields must be non-empty strings")
            if parts not in allowed:
                raise ValueError("delegated rights exceed parent capability")
        try:
            self._authority_key.verify(_b64(signature, 64), _jcs(parent))
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("invalid parent Capability Authority signature") from exc

    def _verify_child(self, child: dict[str, Any], signature: str, parent: dict[str, Any], audience: str, rights: list[dict[str, str]], ttl_seconds: int, now: datetime) -> None:
        try:
            self._authority_key.verify(_b64(signature, 64), _jcs(child))
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("invalid delegated capability signature") from exc
        if not isinstance(child.get("id"), str) or not child["id"].strip():
            raise ValueError("delegated capability id is required")
        if child.get("issuer") != self.authority_issuer or child.get("audience") != audience or child.get("parent_id") != parent["id"]:
            raise ValueError("delegated capability identity or parent binding mismatch")
        if child.get("rights") != rights or child.get("constraints") != parent.get("constraints"):
            raise ValueError("delegated capability rights or constraints mismatch")
        parent_depth = parent.get("delegation_depth", 0)
        if isinstance(parent_depth, bool) or not isinstance(parent_depth, int) or child.get("delegation_depth") != parent_depth + 1:
            raise ValueError("delegated capability depth mismatch")
        issued, expires = child.get("issued_at"), child.get("expires_at")
        parent_expiry = parent.get("expires_at")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (issued, expires, parent_expiry)):
            raise ValueError("delegated capability timestamps are invalid")
        if issued > now.timestamp() + 30 or expires <= now.timestamp() or expires <= issued or expires > parent_expiry or expires - issued > min(ttl_seconds, 300):
            raise ValueError("delegated capability lifetime exceeds parent or request")

    def delegate(
        self, *, parent_capability: dict[str, Any], parent_signature: str,
        audience: str, rights: list[dict[str, str]], ttl_seconds: int = 120,
        correlation_id: str | None = None, timeout: float = 10.0,
        _allow_expired_parent_for_replay: bool = False,
    ) -> dict[str, Any]:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 300:
            raise ValueError("delegated ttl_seconds must be an integer in 1..300")
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("delegated audience is required")
        if not isinstance(rights, list) or not rights:
            raise ValueError("delegated rights are required")
        correlation = correlation_id or str(uuid4())
        if not isinstance(correlation, str) or not correlation.strip() or len(correlation) > 128:
            raise ValueError("correlation_id must contain 1..128 characters")
        if not isinstance(_allow_expired_parent_for_replay, bool):
            raise ValueError("internal replay flag must be boolean")
        current = datetime.now(timezone.utc)
        self._verify_parent(
            parent_capability, parent_signature, rights, current,
            allow_expired=_allow_expired_parent_for_replay,
        )
        args = {
            "parent_capability": parent_capability, "parent_signature": parent_signature,
            "audience": audience, "rights": rights, "ttl_seconds": ttl_seconds,
            "correlation_id": correlation,
        }
        request = {"requester_assertion": self._assertion(current), **args}
        try:
            response = self._http.post(self.endpoint, json=request, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise DelegationUnknownOutcome(f"Capability Authority transport outcome is unknown: {exc}", args) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise DelegationUnknownOutcome("Capability Authority returned non-JSON response", args) from exc
        if response.status_code >= 500:
            raise DelegationUnknownOutcome(f"Capability Authority returned ambiguous status {response.status_code}", args)
        if response.status_code >= 400:
            raise AuthorityRejected(body)
        if not isinstance(body, dict):
            raise DelegationUnknownOutcome("Capability Authority returned a non-object response", args)
        if body.get("status") != "ok":
            raise AuthorityRejected(body)
        if body.get("correlation_id") != correlation:
            raise DelegationUnknownOutcome("Capability Authority response correlation mismatch", args)
        try:
            child, signature = body["capability"], body["signature"]
            self._verify_child(child, signature, parent_capability, audience, rights, ttl_seconds, current)
        except (KeyError, TypeError, ValueError) as exc:
            raise DelegationUnknownOutcome(f"delegated capability could not be independently verified: {exc}", args) from exc
        return body

    def retry(self, unknown: DelegationUnknownOutcome, *, timeout: float = 10.0) -> dict[str, Any]:
        if not isinstance(unknown, DelegationUnknownOutcome):
            raise TypeError("retry requires the DelegationUnknownOutcome from the original attempt")
        return self.delegate(
            **unknown.args_for_retry, timeout=timeout,
            _allow_expired_parent_for_replay=True,
        )


class SwarmClient:
    def __init__(
        self, *, endpoint: str, issuer: str, audience: str,
        capability_authority_issuer: str, issuer_private_key_pem: bytes | str,
        capability_authority_public_key_b64: str,
        swarm_receipt_public_key_pem: bytes | str,
        http_client: httpx.Client | None = None,
        allow_insecure_http: bool = False,
    ):
        parsed_endpoint = httpx.URL(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.host:
            raise ValueError("Swarm endpoint must be an absolute HTTP(S) URL")
        if parsed_endpoint.scheme == "http" and parsed_endpoint.host not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure_http:
            raise ValueError("non-loopback Swarm endpoints require HTTPS")
        self.endpoint = endpoint.rstrip("/") + "/api/integration/enqueue"
        self.issuer, self.audience = issuer, audience
        self.capability_authority_issuer = capability_authority_issuer
        private_bytes = issuer_private_key_pem.encode() if isinstance(issuer_private_key_pem, str) else issuer_private_key_pem
        self._issuer_key = serialization.load_pem_private_key(private_bytes, password=None)
        if not isinstance(self._issuer_key, Ed25519PrivateKey):
            raise ValueError("AVRONE issuer signing key must be Ed25519")
        self._ca_key = Ed25519PublicKey.from_public_bytes(_b64(capability_authority_public_key_b64, 32))
        public_bytes = swarm_receipt_public_key_pem.encode() if isinstance(swarm_receipt_public_key_pem, str) else swarm_receipt_public_key_pem
        self._receipt_key = serialization.load_pem_public_key(public_bytes)
        if not isinstance(self._receipt_key, Ed25519PublicKey):
            raise ValueError("Swarm receipt verification key must be Ed25519")
        self._http = http_client or httpx.Client()

    @classmethod
    def from_env(cls, http_client: httpx.Client | None = None) -> "SwarmClient":
        required = {
            "AVRONE_SWARM_URL": os.getenv("AVRONE_SWARM_URL", ""),
            "AVRONE_SWARM_ISSUER": os.getenv("AVRONE_SWARM_ISSUER", ""),
            "AVRONE_SWARM_AUDIENCE": os.getenv("AVRONE_SWARM_AUDIENCE", ""),
            "AVRONE_SWARM_CA_ISSUER": os.getenv("AVRONE_SWARM_CA_ISSUER", "spiffe://prod/broker"),
            "AVRONE_SWARM_ISSUER_KEY_FILE": os.getenv("AVRONE_SWARM_ISSUER_KEY_FILE", ""),
            "AVRONE_SWARM_CA_PUBLIC_KEY_B64": os.getenv("AVRONE_SWARM_CA_PUBLIC_KEY_B64", ""),
            "AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE": os.getenv("AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE", ""),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError("Swarm client is not configured: " + ", ".join(missing))
        return cls(
            endpoint=required["AVRONE_SWARM_URL"],
            issuer=required["AVRONE_SWARM_ISSUER"],
            audience=required["AVRONE_SWARM_AUDIENCE"],
            capability_authority_issuer=required["AVRONE_SWARM_CA_ISSUER"],
            issuer_private_key_pem=Path(required["AVRONE_SWARM_ISSUER_KEY_FILE"]).read_bytes(),
            capability_authority_public_key_b64=required["AVRONE_SWARM_CA_PUBLIC_KEY_B64"],
            swarm_receipt_public_key_pem=Path(required["AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE"]).read_bytes(),
            http_client=http_client,
            allow_insecure_http=os.getenv("AVRONE_SWARM_ALLOW_HTTP", "0") == "1",
        )

    def _verify_capability(self, capability: dict[str, Any], signature: str, now: datetime) -> None:
        if not isinstance(capability, dict) or capability.get("issuer") != self.capability_authority_issuer or capability.get("audience") != self.audience:
            raise ValueError("capability issuer/audience mismatch")
        if not isinstance(capability.get("id"), str) or not capability["id"].strip():
            raise ValueError("capability id is required")
        issued, expires = capability.get("issued_at"), capability.get("expires_at")
        if isinstance(issued, bool) or isinstance(expires, bool) or not isinstance(issued, (int, float)) or not isinstance(expires, (int, float)):
            raise ValueError("capability timestamps must be numbers")
        if not math.isfinite(issued) or not math.isfinite(expires):
            raise ValueError("capability timestamps must be finite")
        now_seconds = now.timestamp()
        if issued > now_seconds + 30 or expires <= now_seconds or expires <= issued or expires - issued > 300:
            raise ValueError("capability is expired, premature, or exceeds maximum lifetime")
        rights = capability.get("rights")
        if not isinstance(rights, list) or not any(
            isinstance(right, dict) and set(right) == {"type", "resource", "method"}
            and all(right.get(key) == ACTION[key] for key in ACTION) for right in rights
        ):
            raise ValueError("capability does not authorize exact Execute:swarm.jobs:POST")
        try:
            self._ca_key.verify(_b64(signature, 64), _jcs(capability))
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("invalid Capability Authority signature") from exc

    def build_request(
        self, *, payload: dict[str, Any], capability: dict[str, Any],
        capability_signature: str, ttl_seconds: int = 300,
        message_id: str | None = None, correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 300:
            raise ValueError("ttl_seconds must be an integer in 1..300")
        if not isinstance(payload, dict) or not isinstance(payload.get("objective"), str) or not payload["objective"].strip() or len(payload["objective"]) > 2000:
            raise ValueError("payload.objective must contain 1..2000 characters")
        payload = dict(payload)
        if "action" in payload and payload["action"] != ACTION:
            raise ValueError("payload action must be exact Execute:swarm.jobs:POST")
        payload["action"] = dict(ACTION)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._verify_capability(capability, capability_signature, current)
        expires = min(current + timedelta(seconds=ttl_seconds), datetime.fromtimestamp(float(capability["expires_at"]), timezone.utc))
        if expires <= current:
            raise ValueError("capability expires before envelope can be issued")
        unsigned = {
            "schema": ENVELOPE_SCHEMA, "message_id": message_id or str(uuid4()),
            "correlation_id": correlation_id or str(uuid4()), "issued_at": _iso(current),
            "expires_at": _iso(expires), "issuer": self.issuer, "audience": self.audience,
            "capability_id": capability["id"], "payload_digest": _digest(payload),
        }
        signature = base64.b64encode(self._issuer_key.sign(_jcs(unsigned))).decode("ascii")
        return {"envelope": {**unsigned, "signature": signature}, "payload": payload,
                "capability": capability, "capability_signature": capability_signature}

    def verify_receipt(self, receipt: dict[str, Any], request: dict[str, Any]) -> None:
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
            raise ValueError("receipt schema mismatch")
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        try:
            self._receipt_key.verify(_b64(receipt["signature"], 64), _jcs(unsigned))
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("invalid Swarm receipt signature") from exc
        envelope, payload = request["envelope"], request["payload"]
        if receipt["message_id"] != envelope["message_id"] or receipt["correlation_id"] != envelope["correlation_id"]:
            raise ValueError("receipt message/correlation id mismatch")
        if receipt["capability_id"] != envelope["capability_id"] or receipt["payload_digest"] != envelope["payload_digest"] or _digest(payload) != receipt["payload_digest"]:
            raise ValueError("receipt capability/payload digest mismatch")
        if receipt["issuer"] != self.issuer or receipt["executor"] != self.audience or receipt["action"] != "Execute:swarm.jobs:POST":
            raise ValueError("receipt issuer/executor/action mismatch")
        if receipt["resource"] != ACTION["resource"] or receipt["method"] != ACTION["method"] or receipt["outcome"] != "succeeded":
            raise ValueError("receipt does not attest successful enqueue")
        timestamp = datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))
        if _iso(timestamp) != receipt["timestamp"]:
            raise ValueError("receipt timestamp is not canonical UTC")

    def send_request(self, request: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
        try:
            response = self._http.post(self.endpoint, json=request, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UnknownOutcome(f"Swarm transport outcome is unknown: {exc}", request) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise UnknownOutcome("Swarm returned a non-JSON response", request) from exc
        if response.status_code == 409:
            raise ReplayConflict(409, body)
        if response.status_code >= 500:
            raise UnknownOutcome(f"Swarm returned ambiguous server status {response.status_code}", request)
        if response.status_code >= 400:
            raise SwarmRejected(response.status_code, body)
        try:
            job, receipt = body["job"], body["receipt"]
            if not isinstance(job.get("id"), str) or job.get("status") not in {"queued", "running", "completed", "failed"}:
                raise ValueError("invalid job response")
            self.verify_receipt(receipt, request)
        except (KeyError, TypeError, ValueError) as exc:
            raise UnknownOutcome(f"Swarm response could not be independently verified: {exc}", request) from exc
        return body

    def enqueue(self, *, payload: dict[str, Any], capability: dict[str, Any], capability_signature: str, **kwargs: Any) -> dict[str, Any]:
        request = self.build_request(payload=payload, capability=capability, capability_signature=capability_signature, **kwargs)
        return self.send_request(request)

    def retry(self, unknown: UnknownOutcome, *, timeout: float = 10.0) -> dict[str, Any]:
        if not isinstance(unknown, UnknownOutcome):
            raise TypeError("retry requires the UnknownOutcome from the original attempt")
        return self.send_request(unknown.request, timeout=timeout)
    def enqueue_with_delegation(
        self, *, authority_client: CapabilityAuthorityClient,
        parent_capability: dict[str, Any], parent_signature: str,
        payload: dict[str, Any], delegation_correlation_id: str,
        delegation_ttl_seconds: int = 120, **enqueue_kwargs: Any,
    ) -> dict[str, Any]:
        delegated = authority_client.delegate(
            parent_capability=parent_capability,
            parent_signature=parent_signature,
            audience=self.audience,
            rights=[dict(ACTION)],
            ttl_seconds=delegation_ttl_seconds,
            correlation_id=delegation_correlation_id,
        )
        return self.enqueue(
            payload=payload,
            capability=delegated["capability"],
            capability_signature=delegated["signature"],
            **enqueue_kwargs,
        )
