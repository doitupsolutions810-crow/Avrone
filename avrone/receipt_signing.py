"""Client verification for the separate mTLS receipt-signing service."""
from __future__ import annotations

import base64
import json
import os
import ssl
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .task_contract import canonical_digest


class ReceiptSigningError(RuntimeError):
    pass


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def verify_signed_receipt(signed: Mapping[str, Any], *, public_key_b64: str | None = None) -> bool:
    try:
        if not isinstance(signed, Mapping) or set(signed) != {"receipt", "algorithm", "key_id", "signature"}:
            return False
        if signed["algorithm"] != "Ed25519" or not isinstance(signed["key_id"], str) or not signed["key_id"]:
            return False
        receipt = signed["receipt"]
        if not isinstance(receipt, Mapping) or receipt.get("outcome") != "VERIFIED":
            return False
        payload = dict(receipt)
        digest = payload.pop("receipt_sha256", None)
        if not isinstance(digest, str) or canonical_digest(payload) != digest:
            return False
        encoded = public_key_b64 or os.environ.get("AVRONE_RECEIPT_SIGNER_PUBLIC_KEY_B64", "")
        raw_key = base64.b64decode(encoded, validate=True)
        signature = base64.b64decode(signed["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, canonical_receipt_bytes(receipt))
        return True
    except Exception:
        return False


def request_signed_receipt(receipt: Mapping[str, Any], *, base_url: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    url = base_url or os.environ.get("AVRONE_RECEIPT_SIGNER_URL", "")
    parsed = urlsplit(url)
    production = any(os.environ.get(name, "0") == "1" for name in ("PRODUCTION", "AVRONE_PRODUCTION", "PLANE_PRODUCTION", "SPIFFE_REQUIRED"))
    if parsed.scheme == "https":
        cert_file = os.environ.get("AVRONE_RECEIPT_CLIENT_CERT_FILE", "")
        key_file = os.environ.get("AVRONE_RECEIPT_CLIENT_KEY_FILE", "")
        ca_file = os.environ.get("AVRONE_RECEIPT_CA_FILE", "")
        if not cert_file or not key_file:
            raise ReceiptSigningError("receipt signer mTLS client certificate and key are required")
        context = ssl.create_default_context(cafile=ca_file or None)
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    elif not production and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        context = None
    else:
        raise ReceiptSigningError("receipt signer must use HTTPS with mTLS")
    request = Request(url.rstrip("/") + "/v1/receipts/sign", data=json.dumps({"receipt": receipt}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.loads(response.read(128 * 1024))
    except Exception as exc:
        raise ReceiptSigningError("receipt signer request failed") from exc
    signed = payload.get("signed_receipt") if isinstance(payload, dict) else None
    if not verify_signed_receipt(signed):
        raise ReceiptSigningError("receipt signer response failed independent signature verification")
    return dict(signed)
