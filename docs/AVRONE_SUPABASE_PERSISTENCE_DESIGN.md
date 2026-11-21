# AVRONE Supabase persistence design

**Status: PROPOSED / NOT APPLIED.** This is a schema and verification design only. No Supabase project has been restored, no migration has been applied, and no credentials have been read or written.

## Current target availability

The connected Supabase account currently reports these projects as INACTIVE:

- control12-shared-state — candidate by name only; its schema and ownership have not been inspected because the project is inactive.
- supabase-crow-nest
- launch-desk

Do not assume any of these databases is empty or safe to repurpose. Before implementation, the owner must select and authorize a specific active project or approve the cost and restoration of the intended project. Use a development branch for schema validation before any production migration.

## Persistence boundaries

GitHub remains authoritative for pull requests, commit SHAs, workflow runs, checks, and downloadable CI artifacts. Supabase would persist AVRONE's own orchestration state and normalized evidence references:

1. Durable missions and state transitions.
2. Append-only mission events and causal failure chains.
3. Ordered checkpoints for resumable work.
4. Exclusive lane leases with owner fencing and heartbeat expiry.
5. Agent assignments and runner/container metadata.
6. Verification records keyed to repository, exact commit SHA, runtime, and provider run ID.
7. Artifact metadata, content checksums, storage paths, and provenance.
8. Proposal validation records and immutable audit events.

A Supabase record must never overwrite GitHub's source of truth. A CI result for an older or different SHA remains historical evidence, not evidence for the selected head.

## Proposed schema

Use a dedicated, non-exposed schema such as avrone_private, not the default exposed public schema.

| Table | Primary purpose | Important invariants |
|---|---|---|
| missions | Durable mission state and resume metadata | UUID id; repository and target SHA required; monotonic version; idempotency key unique per owner |
| mission_events | Append-only event history and causality | unique (mission_id, sequence_no); immutable event ID; parent/cause references; no update/delete from app roles |
| checkpoints | Durable resume snapshots | unique (mission_id, checkpoint_sequence); snapshot checksum; target SHA; lease epoch captured at write time |
| lane_leases | Exclusive lane ownership | unique (mission_id, lane_name); monotonically increasing lease_epoch; owner, expiry, heartbeat and status |
| agent_assignments | Agent-to-lane and workspace metadata | unique active assignment per mission/agent; normalized lane and agent identifiers |
| verification_runs | Test and deployment evidence | exact 40-character lowercase Git SHA; runtime; provider run ID; check state; started/completed timestamps |
| verification_artifacts | Coverage, Docker, Node and build evidence | immutable content SHA-256; artifact name and provider ID; expiry; private object-storage path |
| proposal_validations | Self-evolution proposal evidence | proposal ID plus exact target SHA; separate validation and acceptance states |
| audit_events | Operator and system audit | append-only actor/action/target/result; correlation ID; redacted metadata |

Use timestamptz for time, UUID primary keys, lowercase snake_case identifiers, explicit foreign keys, and check constraints for enumerated state values. JSONB is for bounded variable payloads, not for fields routinely used in filters or joins.

## Concurrency and durability rules

### Exclusive lane claims

A lane's unique key is (mission_id, lane_name). Claiming is a short transaction that either acquires an unowned/expired lane and increments lease_epoch, or returns a conflict. A worker must present the current epoch when renewing a heartbeat, writing a checkpoint, or finalizing the lane.

A stale worker with epoch n is fenced after a new owner acquires epoch n+1; matching only on agent ID is insufficient. Expiry is compared using database time, not a worker-supplied clock.

### Checkpoints

Write checkpoint payload, checksum, mission version, lane lease epoch, and event record atomically. Use a unique (mission_id, checkpoint_sequence) constraint and optimistic version checks. A duplicate retry with the same idempotency key returns the prior result; a conflicting payload for an existing sequence fails closed.

### Event history

Mission events are append-only and sequence ordered. Do not update historical event rows to fix a bad event; append a compensating/correction event with a causal reference. Store bounded, redacted payloads and external log/artifact references rather than unbounded terminal output.

### Parallel queue work

If workers claim ready tasks from a queue, use a short transaction and SELECT FOR UPDATE SKIP LOCKED with an atomic claim/update. Do not hold database row locks while an agent runs, a container starts, or a remote model responds. Database contention must not be mistaken for a worker failure.

### Exact-SHA verification

A verification record is keyed by repository, full commit SHA, runtime and provider run ID. SHA prefix matches are for search only, never identity. A failed job is FAIL; a running job is UNVERIFIED; no run is NOT RUN. Artifact names are metadata, not proof of artifact contents. Coverage XML/JSON, Docker digests, and Node lifecycle traces require content and provenance validation before their gates can pass.

## Security model

- Keep orchestration tables in a non-exposed schema; do not grant anon access.
- Enable and force RLS on tables as defense in depth, even when the first implementation accesses them only from a trusted server.
- Never ship a Supabase service_role or secret key to browser code. The browser talks to the AVRONE server; the server owns the database connection and credentials.
- If user-scoped access is later introduced, derive identity from auth.uid() and authorization from trusted app metadata or a membership table. Do not authorize from user-editable user_metadata.
- For UPDATE policies, require both USING and WITH CHECK; authentication by itself does not establish row ownership.
- Avoid SECURITY DEFINER functions. If one is proven necessary, keep it in a non-exposed schema, explicitly check caller identity, pin search_path, and revoke unnecessary EXECUTE grants.
- Keep artifact storage private. Validate object path, owner, target SHA, checksum and retention before issuing a download.
- Redact tokens, model credentials, environment variables, and secrets from audit events and logs.

## Index plan

Candidate indexes, subject to observed query patterns:

- missions(owner_id, updated_at DESC) for a user's recent mission list.
- mission_events(mission_id, sequence_no) for event replay.
- checkpoints(mission_id, checkpoint_sequence DESC) for latest-resume lookup.
- lane_leases(expires_at) partial on active leases for cleanup and claim contention.
- verification_runs(repository, target_sha, runtime, started_at DESC) for exact-SHA filtering.
- verification_artifacts(verification_run_id, artifact_name) for gate evidence lookup.

Equality columns precede range/sort columns where appropriate. Add foreign-key indexes for high-volume joins and RLS predicates. Validate with EXPLAIN (ANALYZE, BUFFERS) on representative data before adding speculative indexes.

## Required adversarial acceptance tests

| Test | Expected invariant |
|---|---|
| Two concurrent claims for one lane | Exactly one active owner; the loser receives a conflict |
| Expired lease claimant races old owner | New epoch wins; old epoch cannot heartbeat or write |
| Duplicate heartbeat / idempotent retry | No duplicate state transition |
| Checkpoint same sequence, same payload | Idempotent result |
| Checkpoint same sequence, different payload | Conflict; original checkpoint remains intact |
| Concurrent checkpoints | Monotonic sequence and mission version; no silent overwrite |
| Worker crash between event and checkpoint | Transaction leaves either both committed or neither committed |
| Malformed/oversized JSONB payload | Bounded request rejected without partial state |
| Stale or sibling SHA evidence | Cannot satisfy current target's gate |
| Artifact content hash mismatch | Artifact remains unverified and is quarantined |
| Expired storage object | NOT RUN/EXPIRED; not a pass |
| Cross-owner SELECT/UPDATE/DELETE | RLS denies access |
| UPDATE owner reassignment | WITH CHECK rejects row ownership change |
| Anonymous/unauthenticated request | No mission, checkpoint, event, or artifact data |
| Stale worker epoch | All writes rejected after lease turnover |
| Deadlock / lock timeout | Short transaction retries safely or reports BLOCKED |
| Database disconnect mid-commit | Client does not claim success without a committed read-back |
| Replay with equal timestamps | Sequence number defines stable event ordering |
| Retention boundary | Expiry removes no active mission or referenced artifact |

## Activation and migration sequence

1. Select and authorize the specific Supabase project. Confirm its status, existing schema, migrations, extensions and data before planning DDL.
2. Restore or provision only after the cost and target are explicitly approved. Do not repurpose an inactive project based on its name.
3. Create the migration through the installed Supabase CLI using its migration-new command; do not hand-invent a migration filename.
4. Check the installed CLI version and command help. Supabase CLI v3 is local-first; specify --remote for intended hosted-project operations and do not assume a command targets the linked hosted project.
5. Apply to a development branch first, run schema/RLS tests as owner, service role, authenticated owner, authenticated non-owner and anonymous user, and inspect Supabase security/performance advisors.
6. Validate query plans and concurrent lease/checkpoint tests. Only then prepare a separate production migration for review.

No hosted operation is authorized by this proposal.