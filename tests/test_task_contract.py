"""Deterministic acceptance tests for the task-envelope contract."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from avrone.task_contract import (
    EnvelopeError,
    canonical_digest,
    validate_envelope,
    verify_artifact_bytes,
)


NOW = datetime(2026, 10, 30, 4, 5, tzinfo=timezone.utc)


def valid_envelope():
    return {
        "schema_version": 1,
        "correlation_id": "corr-001",
        "task_id": "task-001",
        "source": {
            "repository": "doitupsolutions810-crow/Avrone",
            "commit_sha": "a" * 40,
        },
        "requested_action": "run_tests",
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "requested_capabilities": ["read_repository"],
        "resource_limits": {
            "timeout_seconds": 120,
            "memory_mb": 512,
            "output_bytes": 65536,
        },
        "input_artifact_digests": ["b" * 64],
        "deadline": (NOW + timedelta(minutes=2)).isoformat(),
    }


def test_valid_envelope_normalizes_and_preserves_source():
    normalized = validate_envelope(valid_envelope(), allowed_capabilities={"read_repository"}, now=NOW)
    assert normalized["schema_version"] == 1
    assert normalized["requested_capabilities"] == ["read_repository"]
    assert normalized["deadline"].endswith("Z")
    assert normalized["source"]["commit_sha"] == "a" * 40


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(schema_version=True),
    lambda e: e.update(requested_action="shell"),
    lambda e: e.update(requested_capabilities=["read_repository", "host_root"]),
    lambda e: e.update(deadline="2026-10-30T04:06:00"),
    lambda e: e.update(deadline=(NOW - timedelta(seconds=1)).isoformat()),
    lambda e: e.update(deadline=(NOW + timedelta(hours=2)).isoformat()),
    lambda e: e["resource_limits"].update(timeout_seconds=True),
    lambda e: e.update(input_artifact_digests=["not-a-digest"]),
    lambda e: e["source"].update(commit_sha="deadbeef"),
    lambda e: e.update(extra_field="must reject"),
])
def test_invalid_envelopes_are_rejected(mutation):
    envelope = valid_envelope()
    mutation(envelope)
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope, allowed_capabilities={"read_repository"}, now=NOW)


def test_non_json_and_nonfinite_values_are_rejected():
    envelope = valid_envelope()
    envelope["resource_limits"]["memory_mb"] = float("nan")
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope, allowed_capabilities={"read_repository"}, now=NOW)


def test_duplicate_capabilities_and_artifacts_are_rejected():
    envelope = valid_envelope()
    envelope["requested_capabilities"] = ["read_repository", "read_repository"]
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope, allowed_capabilities={"read_repository"}, now=NOW)
    envelope = valid_envelope()
    envelope["input_artifact_digests"] = ["b" * 64, "b" * 64]
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope, allowed_capabilities={"read_repository"}, now=NOW)


def test_envelope_size_limit_is_enforced():
    envelope = valid_envelope()
    envelope["correlation_id"] = "x" * 100_000
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope, allowed_capabilities={"read_repository"}, now=NOW)


def test_canonical_digest_is_order_independent_and_changes_with_content():
    assert canonical_digest({"b": 2, "a": 1}) == canonical_digest({"a": 1, "b": 2})
    assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})


def test_artifact_digest_verification():
    payload = b"verified artifact"
    digest = hashlib.sha256(payload).hexdigest()
    assert verify_artifact_bytes(payload, digest)
    assert not verify_artifact_bytes(payload + b"!", digest)
    assert not verify_artifact_bytes(payload, "not-a-digest")


def test_non_mapping_envelope_is_rejected():
    with pytest.raises(EnvelopeError):
        validate_envelope(None, allowed_capabilities=set(), now=NOW)
