"""Tests for fail-closed execution states and receipt integrity."""
import pytest

from avrone.execution_state import (
    StateTransitionError,
    make_receipt,
    transition,
    verify_receipt,
)


def test_valid_state_progression():
    states = [
        ("RECEIVED", "VALIDATED"),
        ("VALIDATED", "AUTHORIZED"),
        ("AUTHORIZED", "DISPATCHED"),
        ("DISPATCHED", "RUNNING"),
        ("RUNNING", "COMPLETED"),
        ("COMPLETED", "VERIFIED"),
    ]
    assert [transition(a, b) for a, b in states] == [b for _, b in states]


@pytest.mark.parametrize("current,target", [
    ("RECEIVED", "RUNNING"),
    ("VALIDATED", "DISPATCHED"),
    ("AUTHORIZED", "COMPLETED"),
    ("COMPLETED", "RUNNING"),
    ("VERIFIED", "RUNNING"),
    ("DENIED", "DISPATCHED"),
    ("RUNNING", "VERIFIED"),
])
def test_illegal_or_terminal_transitions_fail_closed(current, target):
    with pytest.raises(StateTransitionError):
        transition(current, target)


def test_receipt_integrity_detects_mutation():
    receipt = make_receipt(
        correlation_id="corr-1",
        task_id="task-1",
        source_repository="owner/repo",
        source_commit="a" * 40,
        authorization="ALLOW",
        policy_version="policy-v1",
        worker_id="worker-1",
        outcome="VERIFIED",
        artifact_digests=["b" * 64],
        verifier_version="verifier-v1",
    )
    assert verify_receipt(receipt)
    receipt["outcome"] = "WORKER_FAILED"
    assert not verify_receipt(receipt)


def test_verified_receipt_requires_allow_and_worker_identity():
    with pytest.raises(ValueError):
        make_receipt(
            correlation_id="corr-1",
            task_id="task-1",
            source_repository="owner/repo",
            source_commit="a" * 40,
            authorization="NOT_EVALUATED",
            policy_version="policy-v1",
            worker_id=None,
            outcome="VERIFIED",
            artifact_digests=[],
            verifier_version="verifier-v1",
        )


def test_denied_receipt_cannot_claim_success():
    with pytest.raises(ValueError):
        make_receipt(
            correlation_id="corr-1",
            task_id="task-1",
            source_repository="owner/repo",
            source_commit="a" * 40,
            authorization="DENY",
            policy_version="policy-v1",
            worker_id=None,
            outcome="VERIFIED",
            artifact_digests=[],
            verifier_version="verifier-v1",
        )
