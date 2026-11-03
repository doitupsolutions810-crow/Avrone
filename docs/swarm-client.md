# AVRONE → Swarm client

The producer-side transport client is `avrone.swarm_client.SwarmClient`. It creates the versioned Ed25519 envelope, verifies the Capability Authority capability before sending, binds `action` into the signed payload digest, and independently verifies the signed Swarm receipt.

## Configuration

Provision these values through a secret manager or protected environment file. Do not commit private keys.

- `AVRONE_SWARM_URL`: Swarm coordinator base URL (HTTPS required off-loopback).
- `AVRONE_SWARM_ISSUER`: AVRONE workload SPIFFE ID.
- `AVRONE_SWARM_AUDIENCE`: exact Swarm workload SPIFFE ID.
- `AVRONE_SWARM_CA_ISSUER`: pinned Capability Authority issuer ID.
- `AVRONE_SWARM_ISSUER_KEY_FILE`: mounted AVRONE Ed25519 private-key PEM.
- `AVRONE_SWARM_CA_PUBLIC_KEY_B64`: independently pinned raw Ed25519 public key, standard Base64.
- `AVRONE_SWARM_RECEIPT_PUBLIC_KEY_FILE`: mounted Swarm Ed25519 receipt-verification public-key PEM.
- `AVRONE_SWARM_ALLOW_HTTP=1`: development-only override for non-loopback HTTP; never set in production.

## Use

```python
from avrone.swarm_client import SwarmClient, UnknownOutcome

client = SwarmClient.from_env()
# capability + capability_signature must come from the Capability Authority.
try:
    result = client.enqueue(
        payload={"objective": "run the approved specialist task"},
        capability=capability,
        capability_signature=capability_signature,
    )
except UnknownOutcome as unknown:
    # Persist unknown.request in the caller's durable outbox and retry this
    # exact object. Never generate a new message_id after an ambiguous timeout.
    result = client.retry(unknown)
```

The client does not mint capabilities. The current Capability Authority issue #16 tracks authenticated delegation to an explicit Swarm audience. Until that issue is implemented and verified, this client can be exercised with test capabilities but must not be treated as a production-ready live route.

The client is a transport adapter, not an execution engine. A successful signed receipt means the job was durably accepted into Swarm's queue. It does not mean the four role workers have completed real work; their executor adapters remain fail-closed.
