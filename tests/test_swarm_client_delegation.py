from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import rfc8785

from avrone.swarm_client import CapabilityAuthorityClient, DelegationUnknownOutcome


def client_with_http(monkeypatch, response=None):
    client = CapabilityAuthorityClient.__new__(CapabilityAuthorityClient)
    client.requester = "spiffe://prod/agent/avrone"
    client.authority_issuer = "spiffe://prod/broker"
    client.endpoint = "https://ca.example/v1/capabilities/delegate"
    client._verify_parent = lambda *args, **kwargs: None
    client._verify_child = lambda *args: None
    client._requester_key = Ed25519PrivateKey.generate()
    client._assertion = lambda *args: (_ for _ in ()).throw(AssertionError("must reuse supplied assertion"))
    class HTTP:
        def __init__(self):
            self.requests = []
        def post(self, url, json, timeout):
            self.requests.append((url, json, timeout))
            return response or SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "ok", "correlation_id": json["correlation_id"],
                              "capability": {"id": "child"}, "signature": "child-signature"},
            )
    client._http = HTTP()
    return client


def assertion():
    now = datetime.now(timezone.utc)
    issued = now - timedelta(seconds=5)
    expires = issued + timedelta(seconds=30)
    return {
        "schema": "capability-authority.delegation-assertion/v1",
        "issuer": "spiffe://prod/agent/avrone",
        "subject": "spiffe://prod/agent/avrone",
        "audience": "spiffe://prod/broker",
        "jti": "fixed-jti",
        "issued_at": issued.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "signature": "",
    }


def signed_assertion(client):
    value = assertion()
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    value["signature"] = base64.b64encode(client._requester_key.sign(rfc8785.dumps(unsigned))).decode("ascii")
    return value


def test_delegate_uses_persisted_assertion_without_regenerating_it(monkeypatch):
    client = client_with_http(monkeypatch)
    signed = signed_assertion(client)
    result = client.delegate(
        parent_capability={"id": "parent"}, parent_signature="parent-sig",
        audience="spiffe://prod/agent/swarm",
        rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        ttl_seconds=120, correlation_id="stable-correlation",
        requester_assertion=signed,
    )
    sent = client._http.requests[0][1]
    assert sent["requester_assertion"] == signed
    assert result["capability"]["id"] == "child"


@pytest.mark.parametrize("field,value", [
    ("issuer", "spiffe://attacker/agent"),
    ("subject", "spiffe://attacker/agent"),
    ("audience", "spiffe://attacker/ca"),
    ("jti", ""),
    ("signature", ""),
])
def test_delegate_rejects_assertion_identity_or_signature_substitution(monkeypatch, field, value):
    client = client_with_http(monkeypatch)
    bad = signed_assertion(client)
    bad[field] = value
    with pytest.raises(ValueError, match="requester_assertion"):
        client.delegate(
            parent_capability={"id": "parent"}, parent_signature="parent-sig",
            audience="spiffe://prod/agent/swarm",
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
            ttl_seconds=120, correlation_id="stable-correlation",
            requester_assertion=bad,
        )


def test_delegate_rejects_non_object_persisted_assertion(monkeypatch):
    client = client_with_http(monkeypatch)
    with pytest.raises(ValueError, match="requester_assertion"):
        client.delegate(
            parent_capability={"id": "parent"}, parent_signature="parent-sig",
            audience="spiffe://prod/agent/swarm",
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
            ttl_seconds=120, correlation_id="stable-correlation",
            requester_assertion=[],
        )


def test_delegate_rejects_tampered_persisted_assertion_signature(monkeypatch):
    client = client_with_http(monkeypatch)
    bad = signed_assertion(client)
    bad['jti'] = 'attacker-substituted-jti'
    with pytest.raises(ValueError, match='signature verification failed'):
        client.delegate(parent_capability={'id': 'parent'}, parent_signature='parent-sig', audience='spiffe://prod/agent/swarm', rights=[{'type': 'Execute', 'resource': 'swarm.jobs', 'method': 'POST'}], ttl_seconds=120, correlation_id='stable-correlation', requester_assertion=bad)


def test_expired_parent_is_allowed_only_for_explicit_persisted_assertion_replay(monkeypatch):
    client = client_with_http(monkeypatch)
    signed = signed_assertion(client)
    seen = []
    client._verify_parent = lambda *args, **kwargs: seen.append(kwargs.get("allow_expired_for_replay"))
    client.delegate(
        parent_capability={"id": "parent"}, parent_signature="parent-sig",
        audience="spiffe://prod/agent/swarm",
        rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        ttl_seconds=120, correlation_id="stable-correlation",
        requester_assertion=signed, allow_expired_parent_replay=True,
    )
    assert seen == [True]


def test_expired_parent_replay_cannot_mint_a_new_requester_assertion(monkeypatch):
    client = client_with_http(monkeypatch)
    with pytest.raises(ValueError, match="persisted requester assertion"):
        client.delegate(
            parent_capability={"id": "parent"}, parent_signature="parent-sig",
            audience="spiffe://prod/agent/swarm",
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
            ttl_seconds=120, correlation_id="stable-correlation",
            allow_expired_parent_replay=True,
        )


def test_retry_reuses_exact_assertion_and_enables_parent_expiry_reconciliation(monkeypatch):
    client = client_with_http(monkeypatch)
    signed = signed_assertion(client)
    seen = []
    client._verify_parent = lambda *args, **kwargs: seen.append(kwargs.get("allow_expired_for_replay"))
    args = {
        "parent_capability": {"id": "parent"}, "parent_signature": "parent-sig",
        "audience": "spiffe://prod/agent/swarm",
        "rights": [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        "ttl_seconds": 120, "correlation_id": "stable-correlation",
        "requester_assertion": signed,
    }
    unknown = DelegationUnknownOutcome("authority timeout", args)
    client.retry(unknown)
    assert seen == [True]
    assert client._http.requests[0][1]["requester_assertion"] == signed



def test_delegate_rejects_expired_requester_assertion_for_new_delegation(monkeypatch):
    client = client_with_http(monkeypatch)
    stale = signed_assertion(client)
    stale["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    unsigned = {key: item for key, item in stale.items() if key != "signature"}
    stale["signature"] = base64.b64encode(client._requester_key.sign(rfc8785.dumps(unsigned))).decode("ascii")
    with pytest.raises(ValueError, match="requester assertion is expired"):
        client.delegate(
            parent_capability={"id": "parent"}, parent_signature="parent-sig",
            audience="spiffe://prod/agent/swarm",
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
            ttl_seconds=120, correlation_id="stable-correlation",
            requester_assertion=stale,
        )
    assert client._http.requests == []


def test_delegate_allows_expired_requester_assertion_only_for_explicit_replay(monkeypatch):
    client = client_with_http(monkeypatch)
    stale = signed_assertion(client)
    stale["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    unsigned = {key: item for key, item in stale.items() if key != "signature"}
    stale["signature"] = base64.b64encode(client._requester_key.sign(rfc8785.dumps(unsigned))).decode("ascii")
    client.delegate(
        parent_capability={"id": "parent"}, parent_signature="parent-sig",
        audience="spiffe://prod/agent/swarm",
        rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        ttl_seconds=120, correlation_id="stable-correlation",
        requester_assertion=stale, allow_expired_parent_replay=True,
    )
    assert client._http.requests[0][1]["requester_assertion"] == stale


def test_delegate_rejects_requester_assertion_issued_too_far_in_future(monkeypatch):
    client = client_with_http(monkeypatch)
    future = signed_assertion(client)
    issued = datetime.now(timezone.utc) + timedelta(minutes=2)
    future["issued_at"] = issued.isoformat().replace("+00:00", "Z")
    future["expires_at"] = (issued + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    unsigned = {key: item for key, item in future.items() if key != "signature"}
    future["signature"] = base64.b64encode(client._requester_key.sign(rfc8785.dumps(unsigned))).decode("ascii")
    with pytest.raises(ValueError, match="requester assertion timestamps are invalid"):
        client.delegate(
            parent_capability={"id": "parent"}, parent_signature="parent-sig",
            audience="spiffe://prod/agent/swarm",
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
            ttl_seconds=120, correlation_id="stable-correlation",
            requester_assertion=future,
        )
    assert client._http.requests == []

def test_parent_expiry_preflight_can_be_skipped_only_for_replay_after_signature_verification():
    authority_key = Ed25519PrivateKey.generate()
    client = CapabilityAuthorityClient.__new__(CapabilityAuthorityClient)
    client.requester = "spiffe://prod/agent/avrone"
    client.authority_issuer = "spiffe://prod/broker"
    client._authority_key = authority_key.public_key()
    now = datetime.now(timezone.utc)
    parent = {
        "id": "parent-expired", "issuer": client.authority_issuer,
        "audience": "spiffe://prod/agent/avrone",
        "issued_at": now.timestamp() - 120, "expires_at": now.timestamp() - 5,
        "rights": [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
    }
    signature = base64.b64encode(authority_key.sign(rfc8785.dumps(parent))).decode("ascii")
    rights = [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}]
    with pytest.raises(ValueError, match="expired"):
        client._verify_parent(parent, signature, rights, now)
    client._verify_parent(parent, signature, rights, now, allow_expired_for_replay=True)
    invalid_signature = base64.b64encode(b"\\0" * 64).decode("ascii")
    with pytest.raises(ValueError, match="invalid parent Capability Authority signature"):
        client._verify_parent(parent, invalid_signature, rights, now, allow_expired_for_replay=True)


def test_parent_and_delegated_rights_require_nonempty_string_fields():
    authority_key = Ed25519PrivateKey.generate()
    client = CapabilityAuthorityClient.__new__(CapabilityAuthorityClient)
    client.authority_issuer = "spiffe://prod/broker"
    client.requester = "spiffe://prod/agent/avrone"
    client._authority_key = authority_key.public_key()
    now = datetime.now(timezone.utc)
    valid_right = {"type": "Execute", "resource": "swarm.jobs", "method": "POST"}

    malformed_parent = {
        "id": "parent-1", "issuer": client.authority_issuer,
        "audience": client.requester, "issued_at": now.timestamp() - 1,
        "expires_at": now.timestamp() + 60,
        "rights": [{"type": "Execute", "resource": None, "method": "POST"}],
    }
    parent_sig = base64.b64encode(authority_key.sign(rfc8785.dumps(malformed_parent))).decode("ascii")
    with pytest.raises(ValueError, match="exceed parent"):
        client._verify_parent(malformed_parent, parent_sig, [valid_right], now)

    valid_parent = {**malformed_parent, "rights": [valid_right]}
    valid_sig = base64.b64encode(authority_key.sign(rfc8785.dumps(valid_parent))).decode("ascii")
    malformed_right = {"type": "Execute", "resource": None, "method": "POST"}
    with pytest.raises(ValueError, match="non-empty strings"):
        client._verify_parent(valid_parent, valid_sig, [malformed_right], now)
