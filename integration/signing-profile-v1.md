# Cross-repository signing profile v1

**Status:** proposed, fail-closed  
**Applies to:** AVRONE, Capability Authority, Swarm VM Runner, Attested History  
**Signature algorithm:** Ed25519  
**Canonicalization:** RFC 8785 JSON Canonicalization Scheme (JCS)  
**Digest:** SHA-256 over UTF-8 JCS bytes, represented as `sha256:<lowercase hex>`

## Canonical bytes

All signers and verifiers MUST use RFC 8785 JCS. Ordinary language-default JSON serialization is not compatible: Python's default `json.dumps` and JavaScript's `JSON.stringify` differ for exponent notation, negative zero, integral floats, and Unicode escaping. Do not sign pretty-printed JSON, raw transport bytes, or a language-specific object representation.

The payload digest is computed over the complete payload object, including the exact requested action (type, resource, and method). Action metadata MUST NOT be supplied only in an unsigned transport field:

`payload_digest = "sha256:" + hex(SHA256(JCS(payload)))`

The Ed25519 envelope signature is computed over the JCS encoding of the complete object with its `signature` member omitted. Signature bytes are standard padded Base64. Public keys are provisioned out of band and pinned by workload identity and key version. A key returned in the same request/envelope is never a trust root.

## Envelope verification

A receiver MUST:
1. Require exactly the fields in `message-envelope-v1.schema.json`.
2. Verify the pinned issuer key and Ed25519 signature.
3. Require the expected issuer and exact audience.
4. Recompute the payload digest, including the action tuple, and compare it in constant time. Reject any transport-level action that differs from the signed payload.
5. Enforce UTC RFC 3339 timestamps, a maximum 5-minute validity window, bounded clock skew, and expiry.
6. Verify the capability independently against the pinned Capability Authority key, exact audience, expiry, and exact `type/resource/method` tuple.
7. Deduplicate `message_id` and `correlation_id`; identical retries return the original receipt, while changed payload/action/capability under an existing identifier are conflicts.

## Receipt signing

The executor signs the exact receipt schema with its own Ed25519 key. The private key is mounted from a secret manager or protected secret volume; it is never committed, placed in an image, or accepted from the request. The receiver pins the executor public key out of band.

A receipt is not successful until the receipt and corresponding state transition are durably committed. The receipt chain's `previous_receipt_digest` is null for the first receipt and otherwise the SHA-256 JCS digest of the immediately preceding signed receipt. A missing receipt, persistence error, invalid signature, chain break, or ambiguous timeout is not success.

## Test vectors and compatibility

Implementations MUST share test vectors covering:
- nested key ordering and Unicode strings;
- `1e-7`, `1e+21`, integral floating values, and negative zero;
- NaN/Infinity rejection and unpaired surrogate rejection;
- altered payload, signature, issuer, audience, capability, method, expiry, and replay;
- duplicate delivery and conflicting reuse of message/correlation identifiers.

## Deployment gates

- AVRONE issuer public key and Capability Authority public key are separately pinned.
- Swarm executor signing private key is provisioned independently and its public key is registered with AVRONE/Attested History.
- Key IDs, rotation overlap, revocation, and rollback behavior are documented before production.
- No test fixture key or local-development key is trusted in production.
