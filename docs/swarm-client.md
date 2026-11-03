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

## Obtain the child capability through the Authority

When the parent capability addressed to AVRONE is available, use `CapabilityAuthorityClient` to obtain the exact Swarm child capability. The Authority verifies AVRONE's signed 30-second requester assertion against its independently pinned public key, checks the parent capability, and persists the child issuance receipt before returning it.

Additional protected configuration:

- `AVRONE_CA_URL`: Capability Authority base URL (HTTPS off-loopback).
- `AVRONE_CA_ISSUER`: pinned Authority issuer, normally `spiffe://prod/broker`.
- `AVRONE_CA_REQUESTER_ID`: AVRONE workload SPIFFE identity.
- `AVRONE_CA_REQUESTER_KEY_FILE`: AVRONE Ed25519 private key, mounted from the secret manager.
- `AVRONE_CA_PUBLIC_KEY_B64`: independently pinned raw Authority Ed25519 public key.
- `AVRONE_CA_ALLOW_HTTP=1`: development-only loopback override.

```python
from avrone.swarm_client import CapabilityAuthorityClient, SwarmClient

authority = CapabilityAuthorityClient(
    endpoint=ca_url,
    requester=avrone_spiffe_id,
    authority_issuer=ca_issuer,
    requester_private_key_pem=avrone_private_key_pem,
    authority_public_key_b64=ca_public_key_b64,
)
swarm = SwarmClient.from_env()
result = swarm.enqueue_with_delegation(
    authority_client=authority,
    parent_capability=parent_capability,
    parent_signature=parent_signature,
    payload={"objective": "run the approved specialist task"},
    delegation_correlation_id="stable-job-correlation-id",
)
```

Use a stable `delegation_correlation_id` for retries. If the Authority response is lost, call `authority.retry(unknown)` with the returned `DelegationUnknownOutcome`; it refreshes the short-lived requester assertion but preserves the correlation ID so the Authority returns the same persisted child capability. Never generate a new correlation ID for an ambiguous delegation result.

The parent capability must contain `constraints.delegation_audiences` including the exact Swarm SPIFFE ID, and the Authority must independently allowlist that same audience. The parent capability is provisioned out of band by the Authority operator; see [Capability Authority's delegated issuance and bootstrap procedure](https://github.com/doitupsolutions810-crow/capability-authority/blob/security/delegated-audience-issuance-20261103/docs/DELEGATED_CAPABILITY_ISSUANCE.md). The Authority allowlist and public-key mount must be provisioned independently. This client does not mint parent capabilities and does not enable caller override.

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

## Trinity dispatch and durable outbox

Swarm is an explicit dispatch mode, not an automatic replacement for local squads:

```python
result = await trinity.squad(
    "Investigate and fix the approved task",
    run_id="stable-run-identifier",
    dispatch="swarm",
    swarm_correlation_id="stable-job-correlation-id",
)
```

Or set `AVRONE_SQUAD_DISPATCH=swarm` for an operator-configured default. The normal default remains local. Swarm dispatch refuses privileged AVRONE runs and never falls back to local agents when configuration, Authority delegation, or remote enqueue fails.

The outbox is stored under `AVRONE_SWARM_OUTBOX_DIR` (default `~/.avrone/swarm-outbox`). Its directory is mode `0700`; records and lock files are mode `0600`, atomically replaced and fsync-backed. It stores the original task, parent/child capabilities, signed Swarm envelope, correlation ID and last known state. After an ambiguous enqueue timeout, the next call with the same `run_id` or `swarm_correlation_id` retries the exact same envelope. After an ambiguous Authority response, it repeats delegation with the same correlation ID and a fresh short-lived assertion.

The result status is `queued`, `pending`, `rejected`, or `disabled`. `queued` means only that Swarm durably accepted the job. It does not mean the debugger, coder, tester or governance roles have completed.
