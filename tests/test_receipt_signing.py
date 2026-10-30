import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from avrone.execution_state import make_receipt
from avrone.receipt_signing import canonical_receipt_bytes, verify_signed_receipt


def _receipt():
    return make_receipt(
        correlation_id="receipt-corr-1",
        task_id="receipt-task-1",
        source_repository="owner/repo",
        source_commit="a" * 40,
        authorization="ALLOW",
        policy_version="7",
        worker_id="vm-lease-1",
        outcome="VERIFIED",
        artifact_digests=[],
        verifier_version="independent-v1",
    )


def test_signed_receipt_verifies_with_pinned_ed25519_key():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    receipt = _receipt()
    signed = {
        "receipt": receipt,
        "algorithm": "Ed25519",
        "key_id": "unit-test-key-1",
        "signature": base64.b64encode(private.sign(canonical_receipt_bytes(receipt))).decode(),
    }
    public_b64 = base64.b64encode(public).decode()
    assert verify_signed_receipt(signed, public_key_b64=public_b64)


def test_signed_receipt_rejects_tampering_and_wrong_key():
    private = Ed25519PrivateKey.generate()
    wrong = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    receipt = _receipt()
    signed = {
        "receipt": receipt,
        "algorithm": "Ed25519",
        "key_id": "unit-test-key-1",
        "signature": base64.b64encode(private.sign(canonical_receipt_bytes(receipt))).decode(),
    }
    assert not verify_signed_receipt(signed, public_key_b64=base64.b64encode(wrong).decode())
    tampered = dict(signed)
    tampered["receipt"] = {**receipt, "worker_id": "attacker"}
    assert not verify_signed_receipt(tampered, public_key_b64=base64.b64encode(
        private.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    ).decode())


def test_receipt_signer_refuses_unsigned_or_nonverified_receipts():
    private = Ed25519PrivateKey.generate()
    receipt = _receipt()
    receipt["outcome"] = "WORKER_FAILED"
    signature = base64.b64encode(private.sign(canonical_receipt_bytes(receipt))).decode()
    signed = {"receipt": receipt, "algorithm": "Ed25519", "key_id": "k1", "signature": signature}
    public = base64.b64encode(private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )).decode()
    assert not verify_signed_receipt(signed, public_key_b64=public)
