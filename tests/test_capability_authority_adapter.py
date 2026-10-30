from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from avrone import capability_authority as adapter


class FakeResponse:
    status = 200
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.payload[:limit]


def signed_payload(private_key, *, right=None):
    right = right or {"type": "Execute", "resource": "task.run_tests"}
    cap = {
        "id": "cap-test-1",
        "issuer": "spiffe://prod/broker",
        "audience": "spiffe://prod/agent/avrone",
        "trust_domain": "prod",
        "policy_version": 7,
        "expires_at": time.time() + 120,
        "rights": [right],
    }
    signature = base64.b64encode(private_key.sign(adapter._canonical(cap))).decode()
    return {"status": "ok", "capability": cap, "signature": signature}


@pytest.fixture
def envelope():
    return {
        "requested_action": "run_tests",
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "resource_limits": {"timeout_seconds": 90},
    }


@pytest.mark.asyncio
async def test_authority_adapter_verifies_pinned_signature(monkeypatch, envelope):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("AVRONE_PLANE_PUBLIC_KEY_B64", base64.b64encode(pub).decode())
    monkeypatch.setattr(adapter, "urlopen", lambda *a, **k: FakeResponse(signed_payload(key)))
    result = await adapter.authorize_with_capability_authority(envelope)
    assert result["decision"] == "ALLOW"
    assert result["policy_version"] == "7"
    assert result["capability_id"] == "cap-test-1"


@pytest.mark.asyncio
async def test_unsupported_action_denied_without_network(monkeypatch, envelope):
    envelope["requested_action"] = "delete_everything"
    monkeypatch.setattr(adapter, "urlopen", lambda *a, **k: pytest.fail("network must not be called"))
    result = await adapter.authorize_with_capability_authority(envelope)
    assert result == {"decision": "DENY", "reason": "unsupported_action"}


@pytest.mark.asyncio
async def test_invalid_signature_fails_closed(monkeypatch, envelope):
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("AVRONE_PLANE_PUBLIC_KEY_B64", base64.b64encode(pub).decode())
    monkeypatch.setattr(adapter, "urlopen", lambda *a, **k: FakeResponse(signed_payload(other)))
    with pytest.raises(adapter.AuthorityUnavailable):
        await adapter.authorize_with_capability_authority(envelope)
