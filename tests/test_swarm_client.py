import base64
import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest
import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from avrone.swarm_client import SwarmClient, UnknownOutcome

ISSUER = "spiffe://prod/agent/avrone"
AUDIENCE = "spiffe://prod/agent/swarm"
CA_ISSUER = "spiffe://prod/broker"


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def sign_obj(value, key):
    return b64(key.sign(rfc8785.dumps(value)))


@pytest.fixture
def keys():
    issuer, ca, receipt = (Ed25519PrivateKey.generate() for _ in range(3))
    ca_public = ca.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return issuer, ca, receipt, b64(ca_public)


def capability_for(keys, now=None):
    _issuer, ca, _receipt, _pub = keys
    now = (now or datetime.now(timezone.utc)).timestamp()
    capability = {
        "id": "cap-test-1", "issuer": CA_ISSUER, "audience": AUDIENCE,
        "rights": [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        "constraints": {}, "issued_at": now, "expires_at": now + 120,
    }
    return capability, sign_obj(capability, ca)


def client_for(keys, transport=None):
    issuer, _ca, receipt, ca_public = keys
    return SwarmClient(
        endpoint="http://swarm.test", issuer=ISSUER, audience=AUDIENCE,
        capability_authority_issuer=CA_ISSUER,
        issuer_private_key_pem=issuer.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        capability_authority_public_key_b64=ca_public,
        swarm_receipt_public_key_pem=receipt.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
        http_client=httpx.Client(transport=transport) if transport else httpx.Client(),
        allow_insecure_http=True,
    )


def response_for(request_body, receipt_key):
    envelope = request_body["envelope"]
    fields = {
        "message_id": envelope["message_id"], "correlation_id": envelope["correlation_id"],
        "capability_id": envelope["capability_id"], "issuer": ISSUER, "executor": AUDIENCE,
        "action": "Execute:swarm.jobs:POST", "resource": "swarm.jobs", "method": "POST",
        "outcome": "succeeded",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "payload_digest": envelope["payload_digest"], "previous_receipt_digest": None,
    }
    receipt = {**fields, "signature": sign_obj(fields, receipt_key)}
    return httpx.Response(202, json={"job": {"id": "job-1", "status": "queued"}, "receipt": receipt})


def test_builds_signed_envelope_and_binds_action_inside_payload_digest(keys):
    issuer, _ca, _receipt, _pub = keys
    client = client_for(keys)
    capability, signature = capability_for(keys)
    request = client.build_request(payload={"objective": "run all four roles"}, capability=capability, capability_signature=signature)
    envelope = request["envelope"]
    issuer.public_key().verify(base64.b64decode(envelope["signature"]), rfc8785.dumps({k: v for k, v in envelope.items() if k != "signature"}))
    assert request["payload"]["action"] == {"type": "Execute", "resource": "swarm.jobs", "method": "POST"}
    assert envelope["payload_digest"] == "sha256:" + hashlib.sha256(rfc8785.dumps(request["payload"])).hexdigest()


def test_rejects_method_substitution_and_expired_capability(keys):
    client = client_for(keys)
    capability, signature = capability_for(keys)
    with pytest.raises(ValueError, match="exact Execute:swarm.jobs:POST"):
        client.build_request(payload={"objective": "bad", "action": {"type": "Execute", "resource": "swarm.jobs", "method": "DELETE"}}, capability=capability, capability_signature=signature)
    expired, expired_sig = capability_for(keys, datetime(2020, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="expired"):
        client.build_request(payload={"objective": "expired"}, capability=expired, capability_signature=expired_sig)


def test_verifies_signed_enqueue_receipt(keys):
    receipt_key = keys[2]
    transport = httpx.MockTransport(lambda request: response_for(json.loads(request.content), receipt_key))
    client = client_for(keys, transport)
    capability, signature = capability_for(keys)
    result = client.enqueue(payload={"objective": "start the swarm"}, capability=capability, capability_signature=signature)
    assert result["job"]["id"] == "job-1"


def test_unknown_timeout_retries_the_exact_same_signed_request(keys):
    receipt_key, calls = keys[2], []
    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost after commit")
        return response_for(json.loads(request.content), receipt_key)
    client = client_for(keys, httpx.MockTransport(handler))
    capability, signature = capability_for(keys)
    request = client.build_request(payload={"objective": "retry same id"}, capability=capability, capability_signature=signature)
    with pytest.raises(UnknownOutcome) as caught:
        client.send_request(request)
    assert client.retry(caught.value)["job"]["id"] == "job-1"
    assert calls[0] == calls[1]


def test_tampered_receipt_is_not_reported_as_success(keys):
    receipt_key = keys[2]
    def handler(request):
        response = response_for(json.loads(request.content), receipt_key)
        body = response.json()
        body["receipt"]["outcome"] = "failed"
        return httpx.Response(202, json=body)
    client = client_for(keys, httpx.MockTransport(handler))
    capability, signature = capability_for(keys)
    request = client.build_request(payload={"objective": "verify receipt"}, capability=capability, capability_signature=signature)
    with pytest.raises(UnknownOutcome, match="independently verified"):
        client.send_request(request)


def test_enqueue_with_delegation_binds_same_correlation_across_authority_and_swarm(keys):
    capability, signature = capability_for(keys)
    calls = []
    class FakeAuthority:
        def delegate(self, **kwargs):
            calls.append(kwargs)
            return {"capability": capability, "signature": signature}
    transport = httpx.MockTransport(lambda request: response_for(json.loads(request.content), keys[2]))
    client = client_for(keys, transport)
    result = client.enqueue_with_delegation(
        authority_client=FakeAuthority(),
        parent_capability={"id": "parent-cap"},
        parent_signature="parent-signature",
        payload={"objective": "run the four specialist roles"},
        delegation_correlation_id="stable-task-001",
    )
    assert result["job"]["id"] == "job-1"
    assert calls[0]["audience"] == AUDIENCE
    assert calls[0]["rights"] == [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}]
    assert calls[0]["correlation_id"] == "stable-task-001"


def test_enqueue_with_delegation_rejects_correlation_mismatch_before_delegating(keys):
    capability, signature = capability_for(keys)
    class FakeAuthority:
        def delegate(self, **kwargs):
            raise AssertionError("must not delegate with mismatched correlation")
    client = client_for(keys, httpx.MockTransport(lambda request: response_for(json.loads(request.content), keys[2])))
    with pytest.raises(ValueError, match="correlation_id"):
        client.enqueue_with_delegation(
            authority_client=FakeAuthority(),
            parent_capability={"id": "parent-cap"},
            parent_signature="parent-signature",
            payload={"objective": "do not split correlation"},
            delegation_correlation_id="stable-task-001",
            correlation_id="different-task",
        )
