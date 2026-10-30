from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from avrone import capability_authority as authority
from avrone.execution_state import verify_receipt
from avrone.integrated_execution import execute_with_capability_authority


class Response:
    status = 200

    def __init__(self, payload):
        self.data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.data[:limit]


def make_envelope():
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "correlation_id": "integration-corr-1",
        "task_id": "integration-task-1",
        "source": {"repository": "owner/repo", "commit_sha": "a" * 40},
        "requested_action": "run_tests",
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "requested_capabilities": ["read_repository"],
        "resource_limits": {
            "timeout_seconds": 30,
            "memory_mb": 256,
            "output_bytes": 4096,
        },
        "input_artifact_digests": [],
        "deadline": (now + timedelta(seconds=30)).isoformat(),
    }


def install_signed_authority(monkeypatch, private_key):
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(
        "AVRONE_PLANE_PUBLIC_KEY_B64", base64.b64encode(public_bytes).decode()
    )
    monkeypatch.setenv("AVRONE_PLANE_AUDIENCE", "spiffe://prod/agent/avrone")
    monkeypatch.setenv("AVRONE_PLANE_ISSUER", "spiffe://prod/broker")
    monkeypatch.setenv("AVRONE_PLANE_TRUST_DOMAIN", "prod")
    capability = {
        "id": "integration-capability-1",
        "issuer": "spiffe://prod/broker",
        "audience": "spiffe://prod/agent/avrone",
        "trust_domain": "prod",
        "policy_version": 7,
        "expires_at": time.time() + 120,
        "rights": [{"type": "Execute", "resource": "task.run_tests"}],
    }
    signature = base64.b64encode(
        private_key.sign(authority._canonical(capability))
    ).decode()
    monkeypatch.setattr(
        authority,
        "urlopen",
        lambda *args, **kwargs: Response({
            "status": "ok",
            "capability": capability,
            "signature": signature,
        }),
    )


@pytest.mark.asyncio
async def test_integrated_authority_runner_and_independent_artifact_verifier(
    tmp_path, monkeypatch
):
    key = Ed25519PrivateKey.generate()
    install_signed_authority(monkeypatch, key)
    artifact = b"verified integration output"
    digest = hashlib.sha256(artifact).hexdigest()
    runner_calls = []

    async def isolated_runner(task):
        runner_calls.append(task)
        (tmp_path / "result.bin").write_bytes(artifact)
        assert task["_authorization"]["decision"] == "ALLOW"
        assert task["_authorization"]["capability_id"] == "integration-capability-1"
        return {
            "status": "completed",
            "worker_id": "ephemeral-runner-test",
            "artifact_digests": [digest],
            "artifacts": [{"path": "result.bin", "sha256": digest}],
        }

    result = await execute_with_capability_authority(
        make_envelope(),
        allowed_capabilities={"read_repository"},
        run_isolated=isolated_runner,
        authority_url="http://authority.test",
        artifact_root=tmp_path,
    )

    assert result["state"] == "VERIFIED"
    assert len(runner_calls) == 1
    assert result["receipt"]["artifact_digests"] == [digest]
    assert verify_receipt(result["receipt"])


@pytest.mark.asyncio
async def test_invalid_authority_signature_prevents_runner_dispatch(
    tmp_path, monkeypatch
):
    trusted = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    install_signed_authority(monkeypatch, attacker)
    trusted_public = trusted.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(
        "AVRONE_PLANE_PUBLIC_KEY_B64", base64.b64encode(trusted_public).decode()
    )
    calls = []

    async def runner(task):
        calls.append(task)
        return {"status": "completed", "worker_id": "should-not-run", "artifact_digests": [], "artifacts": []}

    result = await execute_with_capability_authority(
        make_envelope(),
        allowed_capabilities={"read_repository"},
        run_isolated=runner,
        authority_url="http://authority.test",
        artifact_root=tmp_path,
    )

    assert result["state"] == "UNKNOWN"
    assert result["reason"] == "authorization_unavailable"
    assert calls == []
