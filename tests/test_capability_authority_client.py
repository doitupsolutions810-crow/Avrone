import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from avrone.swarm_client import AuthorityRejected, CapabilityAuthorityClient, DelegationUnknownOutcome

REQUESTER = "spiffe://prod/agent/avrone"
AUTHORITY = "spiffe://prod/broker"
SWARM = "spiffe://prod/agent/swarm"
RIGHT = {"type": "Execute", "resource": "swarm.jobs", "method": "POST"}


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def sign_obj(value, key):
    return b64(key.sign(rfc8785.dumps(value)))


@pytest.fixture
def setup_client():
    requester = Ed25519PrivateKey.generate()
    authority = Ed25519PrivateKey.generate()
    raw_public = authority.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    client = CapabilityAuthorityClient(
        endpoint="http://localhost:8090",
        requester=REQUESTER,
        authority_issuer=AUTHORITY,
        requester_private_key_pem=requester.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        authority_public_key_b64=b64(raw_public),
        allow_insecure_http=True,
    )
    now = datetime.now(timezone.utc).timestamp()
    parent = {
        "id": "parent-1", "issuer": AUTHORITY, "audience": REQUESTER,
        "rights": [RIGHT], "constraints": {"tenant": "test"},
        "issued_at": now - 10, "expires_at": now + 200, "delegation_depth": 0,
    }
    parent_sig = sign_obj(parent, authority)
    return client, requester, authority, parent, parent_sig


def child_for(parent, authority, now=None, **overrides):
    now = now or datetime.now(timezone.utc).timestamp()
    child = {
        "id": "child-1", "issuer": AUTHORITY, "audience": SWARM,
        "rights": [RIGHT], "constraints": parent["constraints"],
        "issued_at": now, "expires_at": now + 120, "parent_id": parent["id"],
        "delegation_depth": 1, "trust_domain": "prod", "policy_version": 1,
    }
    child.update(overrides)
    return child, sign_obj(child, authority)


def test_signs_requester_assertion_and_verifies_child_capability(setup_client):
    client, requester, authority, parent, parent_sig = setup_client
    captured = {}
    def handler(request):
        body = request.read()
        import json
        payload = json.loads(body)
        captured.update(payload)
        assertion = payload["requester_assertion"]
        signature = base64.b64decode(assertion.pop("signature"))
        requester.public_key().verify(signature, rfc8785.dumps(assertion))
        assertion["signature"] = b64(signature)
        assert assertion["issuer"] == REQUESTER
        assert assertion["subject"] == REQUESTER
        assert assertion["audience"] == AUTHORITY
        assert payload["audience"] == SWARM
        child, signature = child_for(parent, authority)
        return httpx.Response(200, json={"status": "ok", "correlation_id": payload["correlation_id"], "capability": child, "signature": signature, "idempotent_replay": False})
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    result = client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT], correlation_id="job-1")
    assert result["capability"]["audience"] == SWARM
    assert captured["correlation_id"] == "job-1"


def test_rejects_wrong_parent_audience_and_rights_before_network(setup_client):
    client, _requester, _authority, parent, parent_sig = setup_client
    calls = []
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="issuer/audience"):
        client.delegate(parent_capability={**parent, "audience": "spiffe://prod/agent/attacker"}, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT])
    with pytest.raises(ValueError, match="exceed parent"):
        client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "DELETE"}])
    assert not calls


def test_unknown_outcome_retry_reuses_correlation_with_fresh_assertion(setup_client):
    client, _requester, authority, parent, parent_sig = setup_client
    calls = []
    def handler(request):
        import json
        body = json.loads(request.read())
        calls.append(body)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost")
        child, signature = child_for(parent, authority)
        return httpx.Response(200, json={"status": "ok", "correlation_id": body["correlation_id"], "capability": child, "signature": signature})
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DelegationUnknownOutcome) as caught:
        client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT], correlation_id="stable-job-2")
    result = client.retry(caught.value)
    assert result["capability"]["parent_id"] == parent["id"]
    assert calls[0]["correlation_id"] == calls[1]["correlation_id"] == "stable-job-2"
    assert calls[0]["requester_assertion"]["jti"] != calls[1]["requester_assertion"]["jti"]


def test_rejects_authority_policy_error_and_malformed_child(setup_client):
    client, _requester, authority, parent, parent_sig = setup_client
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "error", "error": "audience not allowlisted"})))
    with pytest.raises(AuthorityRejected):
        client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT])
    child, signature = child_for(parent, authority, audience="spiffe://prod/agent/attacker")
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok", "correlation_id": "corr-malformed", "capability": child, "signature": signature})))
    with pytest.raises(DelegationUnknownOutcome, match="independently verified"):
        client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT], correlation_id="corr-malformed")


def test_rejects_invalid_ttl_and_noncanonical_parent_signature(setup_client):
    client, _requester, _authority, parent, parent_sig = setup_client
    with pytest.raises(ValueError, match="ttl_seconds"):
        client.delegate(parent_capability=parent, parent_signature=parent_sig, audience=SWARM, rights=[RIGHT], ttl_seconds=True)
    with pytest.raises(ValueError, match="parent"):
        client.delegate(parent_capability=parent, parent_signature="bad", audience=SWARM, rights=[RIGHT])
