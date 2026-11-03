from types import SimpleNamespace

import pytest

from avrone.swarm_client import CapabilityAuthorityClient


def client_with_http(monkeypatch, response=None):
    client = CapabilityAuthorityClient.__new__(CapabilityAuthorityClient)
    client.requester = "spiffe://prod/agent/avrone"
    client.authority_issuer = "spiffe://prod/broker"
    client.endpoint = "https://ca.example/v1/capabilities/delegate"
    client._verify_parent = lambda *args: None
    client._verify_child = lambda *args: None
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
    return {
        "schema": "capability-authority.delegation-assertion/v1",
        "issuer": "spiffe://prod/agent/avrone",
        "subject": "spiffe://prod/agent/avrone",
        "audience": "spiffe://prod/broker",
        "jti": "fixed-jti",
        "issued_at": "2026-11-03T09:00:00.000Z",
        "expires_at": "2026-11-03T09:00:30.000Z",
        "signature": "fixed-signature",
    }


def test_delegate_uses_persisted_assertion_without_regenerating_it(monkeypatch):
    client = client_with_http(monkeypatch)
    signed_assertion = assertion()
    result = client.delegate(
        parent_capability={"id": "parent"}, parent_signature="parent-sig",
        audience="spiffe://prod/agent/swarm",
        rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        ttl_seconds=120, correlation_id="stable-correlation",
        requester_assertion=signed_assertion,
    )
    sent = client._http.requests[0][1]
    assert sent["requester_assertion"] == signed_assertion
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
    bad = assertion()
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
