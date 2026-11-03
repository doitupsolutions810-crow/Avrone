"""Real Ed25519 contract tests for AVRONE's Capability Authority client."""
import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from avrone import swarm_client as sc

REQUESTER = "spiffe://prod/agent/avrone"
BROKER = "spiffe://prod/broker"
SWARM = "spiffe://prod/agent/swarm"
RIGHT = {"type": "Execute", "resource": "swarm.jobs", "method": "POST"}


class Response:
    status_code = 200
    def __init__(self, body):
        self.body = body
    def json(self):
        return self.body


class StubHTTP:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def pem_private(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def b64_public(key):
    raw = key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def make_parent(ca_private, now, expires_in=300):
    parent = {
        "id": "parent-avrone-1", "issuer": BROKER, "audience": REQUESTER,
        "rights": [RIGHT],
        "constraints": {"tenant": "avrone-swarm", "delegation_audiences": [SWARM]},
        "issued_at": now.timestamp() - 1,
        "expires_at": now.timestamp() + expires_in,
        "delegation_depth": 0,
    }
    signature = base64.b64encode(ca_private.sign(sc._jcs(parent))).decode("ascii")
    return parent, signature


def make_client(http, requester_private, ca_private):
    return sc.CapabilityAuthorityClient(
        endpoint="http://127.0.0.1:8090",
        requester=REQUESTER,
        authority_issuer=BROKER,
        requester_private_key_pem=pem_private(requester_private),
        authority_public_key_b64=b64_public(ca_private.public_key()),
        http_client=http,
    )


def signed_child(ca_private, parent, now, expires_in=60):
    child = {
        "id": "child-avrone-1", "issuer": BROKER, "audience": SWARM,
        "rights": [RIGHT], "constraints": parent["constraints"],
        "issued_at": now.timestamp(), "expires_at": now.timestamp() + expires_in,
        "parent_id": parent["id"], "delegation_depth": 1,
    }
    signature = base64.b64encode(ca_private.sign(sc._jcs(child))).decode("ascii")
    return child, signature


def test_delegate_signs_requester_assertion_and_verifies_authority_child():
    now = datetime.now(timezone.utc)
    requester = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    parent, parent_sig = make_parent(ca, now)
    child, child_sig = signed_child(ca, parent, now)
    http = StubHTTP([Response({
        "status": "ok", "correlation_id": "corr-success",
        "capability": child, "signature": child_sig,
    })])
    client = make_client(http, requester, ca)
    result = client.delegate(
        parent_capability=parent, parent_signature=parent_sig,
        audience=SWARM, rights=[RIGHT], ttl_seconds=120,
        correlation_id="corr-success",
    )
    assert result["capability"] == child
    url, request, _timeout = http.calls[0]
    assert url.endswith("/v1/capabilities/delegate")
    assertion = request["requester_assertion"]
    assert assertion["issuer"] == REQUESTER
    assert assertion["subject"] == REQUESTER
    assert assertion["audience"] == BROKER
    unsigned = {k: v for k, v in assertion.items() if k != "signature"}
    requester.public_key().verify(
        sc._b64(assertion["signature"], 64), sc._jcs(unsigned)
    )


def test_timeout_retry_reuses_correlation_and_can_reconcile_expired_parent(monkeypatch):
    real_now = datetime.now(timezone.utc)
    requester = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    parent, parent_sig = make_parent(ca, real_now, expires_in=1)
    http = StubHTTP([
        httpx.ReadTimeout("response lost"),
        Response({"status": "error", "error": "delegated_capability_expired",
                  "correlation_id": "corr-timeout"}),
    ])
    client = make_client(http, requester, ca)

    class Clock:
        values = [real_now, real_now + timedelta(seconds=2)]
        @classmethod
        def now(cls, tz=None):
            return cls.values.pop(0)

    monkeypatch.setattr(sc, "datetime", Clock)
    with pytest.raises(sc.DelegationUnknownOutcome) as caught:
        client.delegate(
            parent_capability=parent, parent_signature=parent_sig,
            audience=SWARM, rights=[RIGHT], ttl_seconds=120,
            correlation_id="corr-timeout",
        )
    with pytest.raises(sc.AuthorityRejected, match="delegated_capability_expired"):
        client.retry(caught.value)

    assert len(http.calls) == 2
    first, second = http.calls[0][1], http.calls[1][1]
    assert first["correlation_id"] == second["correlation_id"] == "corr-timeout"
    assert first["requester_assertion"] == second["requester_assertion"]


def test_direct_delegate_never_uses_expired_parent_for_new_issuance(monkeypatch):
    now = datetime.now(timezone.utc)
    requester = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    parent, parent_sig = make_parent(ca, now, expires_in=1)
    http = StubHTTP([])
    client = make_client(http, requester, ca)

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(seconds=2)

    monkeypatch.setattr(sc, "datetime", Clock)
    with pytest.raises(ValueError, match="expired"):
        client.delegate(
            parent_capability=parent, parent_signature=parent_sig,
            audience=SWARM, rights=[RIGHT], ttl_seconds=30,
            correlation_id="corr-expired-new",
        )
    assert http.calls == []


def test_authority_response_correlation_mismatch_is_unknown_outcome():
    now = datetime.now(timezone.utc)
    requester = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    parent, parent_sig = make_parent(ca, now)
    http = StubHTTP([Response({"status": "ok", "correlation_id": "other"})])
    client = make_client(http, requester, ca)
    with pytest.raises(sc.DelegationUnknownOutcome, match="correlation mismatch"):
        client.delegate(
            parent_capability=parent, parent_signature=parent_sig,
            audience=SWARM, rights=[RIGHT], ttl_seconds=30,
            correlation_id="corr-expected",
        )


def test_parent_method_substitution_is_rejected_before_network():
    now = datetime.now(timezone.utc)
    requester = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    parent, parent_sig = make_parent(ca, now)
    http = StubHTTP([])
    client = make_client(http, requester, ca)
    with pytest.raises(ValueError, match="exceed parent"):
        client.delegate(
            parent_capability=parent, parent_signature=parent_sig,
            audience=SWARM,
            rights=[{"type": "Execute", "resource": "swarm.jobs", "method": "DELETE"}],
            ttl_seconds=30, correlation_id="corr-method",
        )
    assert http.calls == []


def test_parent_credential_loader_accepts_private_canonical_file(tmp_path):
    import json
    from avrone.swarm_client import load_parent_capability_file
    tmp_path.chmod(0o700)
    capability = {"id": "parent-1", "audience": REQUESTER}
    signature = base64.b64encode(b"x" * 64).decode("ascii")
    path = tmp_path / "parent.json"
    path.write_text(json.dumps({"capability": capability, "signature": signature}))
    path.chmod(0o600)
    assert load_parent_capability_file(path) == (capability, signature)


def test_parent_file_alias_keeps_protected_loader_and_rejects_conflicts(tmp_path, monkeypatch):
    import json
    from avrone.swarm_client import load_parent_capability_file
    tmp_path.chmod(0o700)
    path = tmp_path / "parent.json"
    signature = base64.b64encode(b"x" * 64).decode("ascii")
    path.write_text(json.dumps({"capability": {"id": "parent"}, "signature": signature}))
    path.chmod(0o600)
    monkeypatch.delenv("AVRONE_SWARM_PARENT_CAPABILITY_FILE", raising=False)
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(path))
    assert load_parent_capability_file() == ({"id": "parent"}, signature)
    monkeypatch.setenv("AVRONE_SWARM_PARENT_CAPABILITY_FILE", str(tmp_path / "other.json"))
    with pytest.raises(RuntimeError, match="conflict"):
        load_parent_capability_file()
    monkeypatch.setenv("AVRONE_SWARM_PARENT_CAPABILITY_FILE", str(path))
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        load_parent_capability_file()


def test_parent_credential_loader_rejects_symlink_and_permissive_mode(tmp_path):
    from avrone.swarm_client import load_parent_capability_file
    tmp_path.chmod(0o700)
    real = tmp_path / "real.json"
    real.write_text('{"capability":{},"signature":"' + base64.b64encode(b"x" * 64).decode() + '"}')
    real.chmod(0o600)
    link = tmp_path / "parent.json"
    link.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        load_parent_capability_file(link)
    link.unlink()
    real.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        load_parent_capability_file(real)


def test_parent_credential_loader_rejects_duplicate_keys_and_oversize(tmp_path):
    from avrone.swarm_client import load_parent_capability_file
    tmp_path.chmod(0o700)
    path = tmp_path / "parent.json"
    path.write_text('{"capability":{},"capability":{},"signature":"' + base64.b64encode(b"x" * 64).decode() + '"}')
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="valid bounded JSON"):
        load_parent_capability_file(path)
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(RuntimeError, match="size limit"):
        load_parent_capability_file(path)


def test_authority_client_from_env_uses_pinned_files_and_key(tmp_path, monkeypatch):
    from avrone.swarm_client import CapabilityAuthorityClient
    tmp_path.chmod(0o700)
    requester = Ed25519PrivateKey.generate()
    authority = Ed25519PrivateKey.generate()
    key_file = tmp_path / "requester.pem"
    key_file.write_bytes(pem_private(requester))
    key_file.chmod(0o600)
    monkeypatch.setenv("AVRONE_CA_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("AVRONE_CA_REQUESTER_ID", REQUESTER)
    monkeypatch.setenv("AVRONE_CA_REQUESTER_KEY_FILE", str(key_file))
    monkeypatch.setenv("AVRONE_CA_PUBLIC_KEY_B64", b64_public(authority.public_key()))
    monkeypatch.setenv("AVRONE_CA_ALLOW_HTTP", "1")
    client = CapabilityAuthorityClient.from_env(http_client=StubHTTP([]))
    assert client.requester == REQUESTER
    assert client.authority_issuer == BROKER
    assert isinstance(client._requester_key, Ed25519PrivateKey)


def test_swarm_client_from_env_loads_bounded_private_files(tmp_path, monkeypatch):
    from avrone.swarm_client import SwarmClient
    tmp_path.chmod(0o700)
    issuer = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    receipt = Ed25519PrivateKey.generate()
    issuer_file = tmp_path / "issuer.pem"
    receipt_file = tmp_path / "receipt.pub"
    issuer_file.write_bytes(pem_private(issuer))
    receipt_file.write_bytes(receipt.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    issuer_file.chmod(0o600)
    receipt_file.chmod(0o600)
    monkeypatch.setenv("AVRONE_SWARM_URL", "https://swarm.example.internal")
    monkeypatch.setenv("AVRONE_SWARM_ISSUER", "spiffe://prod/agent/avrone-swarm-producer")
    monkeypatch.setenv("AVRONE_SWARM_AUDIENCE", SWARM)
    monkeypatch.setenv("AVRONE_SWARM_ISSUER_KEY_FILE", str(issuer_file))
    monkeypatch.setenv("AVRONE_SWARM_CA_PUBLIC_KEY_B64", b64_public(ca.public_key()))
    monkeypatch.setenv("AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE", str(receipt_file))
    client = SwarmClient.from_env()
    assert isinstance(client._issuer_key, Ed25519PrivateKey)
    assert isinstance(client._receipt_key, Ed25519PublicKey)
    client._http.close()


def test_swarm_client_from_env_rejects_permissive_or_symlinked_keys(tmp_path, monkeypatch):
    from avrone.swarm_client import SwarmClient
    tmp_path.chmod(0o700)
    issuer = Ed25519PrivateKey.generate()
    ca = Ed25519PrivateKey.generate()
    issuer_file = tmp_path / "issuer.pem"
    receipt_real = tmp_path / "receipt-real.pub"
    receipt_link = tmp_path / "receipt.pub"
    issuer_file.write_bytes(pem_private(issuer))
    receipt_real.write_bytes(issuer.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    issuer_file.chmod(0o644)
    receipt_real.chmod(0o600)
    receipt_link.symlink_to(receipt_real)
    for key, value in {
        "AVRONE_SWARM_URL": "https://swarm.example.internal",
        "AVRONE_SWARM_ISSUER": "spiffe://prod/agent/avrone-swarm-producer",
        "AVRONE_SWARM_AUDIENCE": SWARM,
        "AVRONE_SWARM_ISSUER_KEY_FILE": str(issuer_file),
        "AVRONE_SWARM_CA_PUBLIC_KEY_B64": b64_public(ca.public_key()),
        "AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE": str(receipt_link),
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="0600"):
        SwarmClient.from_env()
    issuer_file.chmod(0o600)
    with pytest.raises(RuntimeError, match="symlink"):
        SwarmClient.from_env()
