# Capability Authority mTLS integration

AVRONE's `execute_with_capability_authority` entry point composes the signed capability client, task contract, injected isolated runner, and independent staged-byte verifier.

## AVRONE environment

- `AVRONE_CAPABILITY_AUTHORITY_URL`: HTTPS URL of the capability authority.
- `AVRONE_PLANE_CA_FILE`: CA bundle that validates the authority's TLS certificate.
- `AVRONE_PLANE_CLIENT_CERT_FILE` and `AVRONE_PLANE_CLIENT_KEY_FILE`: current AVRONE workload X.509-SVID and private key for mutual TLS.
- `AVRONE_PLANE_PUBLIC_KEY_B64`: out-of-band pinned raw Ed25519 broker signing public key, base64 encoded.
- `AVRONE_PLANE_ISSUER`, `AVRONE_PLANE_TRUST_DOMAIN`, and `AVRONE_PLANE_AUDIENCE`: expected signed capability issuer, trust domain, and this AVRONE workload's SPIFFE ID.
- `AVRONE_ARTIFACT_ROOT`: confined directory where the runner stages artifacts.
- `AVRONE_PRODUCTION=1`: forbids plain HTTP; production requires HTTPS and client certificate/key.

The TLS certificate and key must be provisioned and rotated by the workload identity system. Do not commit them or place them in model-visible configuration.

## Capability Authority environment

Set `PRODUCTION=1` (or `PLANE_PRODUCTION=1` / `SPIFFE_REQUIRED=1`) and configure:

- `PLANE_TLS_CERT_FILE` and `PLANE_TLS_KEY_FILE`: broker workload X.509-SVID and private key.
- `PLANE_TLS_CLIENT_CA_FILE`: trust bundle used to validate AVRONE client SVIDs.
- `PLANE_ALLOWED_TRUST_DOMAINS` and `PLANE_TRUST_DOMAIN`: approved SPIFFE trust domain(s).

The production issue route accepts the caller only from the verified TLS peer certificate's single SPIFFE URI SAN, restricted to the configured trust domain and `spiffe://<domain>/agent/` path. A JSON `caller` value is overwritten and is never trusted. Production startup fails if the mTLS certificate, key, or client CA is absent.

## Runner contract and release gate

`execute_with_capability_authority` deliberately requires a `run_isolated` adapter. That adapter must provision one disposable VM per job, enforce timeout/memory/output/network policy, stage output under the configured artifact root, and destroy the VM in a `finally` path. The repository's current Compose runner is not that VM adapter.

The integration test uses a fake isolated runner and a mocked HTTP response carrying a real Ed25519 signature. It proves AVRONE's client-to-orchestrator contract and independent artifact-byte hashing; it does not prove a deployed mTLS connection, VM isolation, or a signed receipt. The current receipt carries a SHA-256 integrity digest, not a digital signature. Keep autonomous publication disabled until those remaining gates are implemented and exercised against live services.
