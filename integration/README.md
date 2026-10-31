# AVRONE cross-repository integration contract

**Status:** proposal / fail-closed  
**Version:** 1.0.0  
**Authority:** Control12 + Capability Authority  
**Promotion state:** HOLD until each contract edge has independent runtime evidence.

## Purpose

This contract replaces implicit repository coupling with explicit, versioned interfaces. A repository existing, a branch being present, or a README claiming integration is not proof that two components interoperate.

## Initial control-plane participants

| Repository | Responsibility | Required contract |
|---|---|---|
| `Avrone` | supervisor, orchestration, integration registry | task envelope, run state, evidence receipt |
| `TrainLLM` | inference / analysis | model request, model response, provenance |
| `capability-authority` | identity, rights, issue/verify/execute | signed capability, exact method, receipt |
| `control12-lattice-ops` | policy admission and release planning | intent, policy decision, residual evidence |
| `attested-history` | append-only history verification | canonical entry, parent digest, signer identity |
| `swarm-vm-runner` | isolated worker execution | job ID, worker identity, terminal state |
| `CycleKernal` | deterministic cycle / state replay | epoch, state digest, replay receipt |

This is the first control slice, not a claim that it represents all 36 candidate repositories. The full repository inventory must be reconciled against actual branch tips before adding more participants.

## Mandatory envelope

Every cross-repository request must carry:
- `schema`: immutable, versioned schema identifier.
- `message_id`: globally unique idempotency key.
- `correlation_id`: end-to-end workflow identifier.
- `issued_at` and `expires_at`: bounded validity window.
- `issuer` and `audience`: independently authenticated workload identities.
- `capability_id`: reference to a signed, action/resource/method-scoped capability.
- `payload_digest`: digest over canonical payload bytes.
- `signature`: signature from an independently pinned issuer key.

A message missing any required field is rejected. Embedded public keys are descriptive metadata, never trust anchors.

## Receipt requirements

A successful side effect must return a signed receipt containing the request ID, capability ID, issuer, executor, exact action/resource/method, outcome, timestamp, payload digest, and previous receipt digest. A timeout or missing receipt is an unknown outcome, not success. Retries reuse the same idempotency key.

## Promotion gates

1. **Inventory:** repository and branch SHA pinned; ownership and role confirmed.
2. **Contract:** schemas and compatibility policy reviewed by both producers and consumers.
3. **Identity:** workload identity and signer key independently pinned; key rotation documented.
4. **Authorization:** capability checked at execution time against exact action, resource, and method.
5. **Evidence:** durable receipt persisted before success is reported.
6. **Replay:** duplicate, stale, reordered, expired, and tampered messages rejected or safely deduplicated.
7. **Reliability:** retry, timeout, partial failure, and recovery tested.
8. **Independent verification:** consumer-side tests and a separate verifier approve the evidence.
9. **Promotion:** policy engine approves; proposing agent cannot approve its own change.
10. **Learning:** only verified outcomes may become reusable skills; policy, trust roots, tests, and holdout cases are immutable to the learning agent.

## Safe connection sequence

`inventory -> contract freeze -> trust roots -> capability enforcement -> receipt durability -> adapters -> cross-repo tests -> independent attestation -> promotion`

No direct agent-to-agent shell, shared mutable filesystem, or implicit credential inheritance is allowed. Agents propose work; the capability authority decides whether it may execute. The orchestrator never treats a generated summary as a test result.

## Success criteria

- 100% of active edges have pinned source revisions and versioned schemas.
- 100% of mutating actions have exact-method authorization.
- 100% of successful side effects have durable, verifiable receipts.
- 0 self-signed trust roots accepted.
- 0 unmeasured or missing residual values treated as a passing state.
- 100% of retryable actions are idempotent.
- All negative tests pass: forged signer, action substitution, method substitution, replay, expiry, duplicate delivery, missing receipt, and stale source revision.
- Every release has a reproducible test report and an independent promotion decision.

## Current disposition

All cross-repository edges remain **UNVERIFIED / HOLD** until the gates above are satisfied. A green unit-test suite in one repository does not establish end-to-end integration. The current TrainLLM and Capability Authority security PRs are draft and await CI confirmation and operator review.
