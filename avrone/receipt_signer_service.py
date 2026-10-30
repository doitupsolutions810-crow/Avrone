"""Standalone mTLS receipt signer. Run in a separate, trusted process."""
from __future__ import annotations

import base64
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .execution_state import verify_receipt
from .receipt_signing import canonical_receipt_bytes


def load_signing_key() -> Ed25519PrivateKey:
    path = os.environ.get("RECEIPT_SIGNER_PRIVATE_KEY_FILE", "")
    if not path:
        raise SystemExit("RECEIPT_SIGNER_PRIVATE_KEY_FILE is required")
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("receipt signer key must be Ed25519")
    return key


class Handler(BaseHTTPRequestHandler):
    server_version = "AVRONE-Receipt-Signer"

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/v1/receipts/sign":
            self.reply(404, {"error": "not_found"})
            return
        try:
            cert = self.connection.getpeercert()
            uris = [v for kind, v in (cert or {}).get("subjectAltName", []) if kind == "URI" and v.startswith("spiffe://")]
            allowed = os.environ.get("RECEIPT_SIGNER_ALLOWED_CALLER", "spiffe://prod/agent/avrone")
            if uris != [allowed]:
                self.reply(403, {"error": "caller_not_authorized"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 128 * 1024:
                self.reply(413, {"error": "invalid_body_size"})
                return
            request = json.loads(self.rfile.read(length))
            receipt = request.get("receipt") if isinstance(request, dict) else None
            if not isinstance(receipt, dict) or receipt.get("outcome") != "VERIFIED" or not verify_receipt(receipt):
                self.reply(400, {"error": "invalid_verified_receipt"})
                return
            key = self.server.signing_key
            signature = base64.b64encode(key.sign(canonical_receipt_bytes(receipt))).decode()
            public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            key_id = os.environ.get("RECEIPT_SIGNER_KEY_ID", "receipt-ed25519-v1")
            self.reply(200, {"signed_receipt": {"receipt": receipt, "algorithm": "Ed25519", "key_id": key_id, "signature": signature}, "public_key_b64": base64.b64encode(public).decode()})
        except Exception:
            self.reply(400, {"error": "invalid_request"})

    def log_message(self, format, *args):
        return


def main() -> None:
    cert = os.environ.get("RECEIPT_SIGNER_TLS_CERT_FILE", "")
    key = os.environ.get("RECEIPT_SIGNER_TLS_KEY_FILE", "")
    ca = os.environ.get("RECEIPT_SIGNER_CLIENT_CA_FILE", "")
    if not cert or not key or not ca:
        raise SystemExit("receipt signer requires TLS cert, key, and client CA files")
    server = HTTPServer((os.environ.get("RECEIPT_SIGNER_HOST", "127.0.0.1"), int(os.environ.get("RECEIPT_SIGNER_PORT", "8444"))), Handler)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=cert, keyfile=key)
    context.load_verify_locations(cafile=ca)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.signing_key = load_signing_key()
    server.serve_forever()


if __name__ == "__main__":
    main()
