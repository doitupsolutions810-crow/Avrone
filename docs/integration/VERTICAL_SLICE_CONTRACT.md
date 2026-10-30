# Cross-repository Vertical Slice Contract

Status: integration contract / implementation plan. This document does not claim that the complete stack has been deployed or passed end-to-end tests.

## Goal

Prove one request can travel from Grok Bot chat through AVRONE, receive a control-plane authorization decision, execute in an isolated worker, and return a verifiable receipt. Every stage must carry the same correlation ID and immutable source revision metadata.

## Component ownership

- Grok Bot: user-facing chat and model/tool orchestration. It must not receive host-level execution privileges.
- AVRONE: fixed-route bridge, task envelope validation, orchestration, bounded queueing, event logging, and result normalization.
- Control plane: authoritative policy decision. The caller cannot select or override its tenant, identity, policy, or capability allowlist.
- VM/worker runner: executes only an approved envelope in a disposable or explicitly isolated environment with bounded CPU, memory, time, output size, and network access.
- Evidence ledger: records request hash, correlation ID, policy decision, worker identity, source revision, start/end timestamps, exit status, artifact digests, and verifier outcome.

## Canonical task envelope

Required fields:

- schema_version: fixed supported version
- correlation_id: unpredictable unique identifier
- task_id: unique idempotency key
- source: repository and immutable commit SHA
- requested_action: enumerated operation, never an arbitrary command string from an untrusted caller
- tenant_id and principal_id: established by authenticated server context, not trusted from the model
- requested_capabilities: checked as a subset of operator-configured capabilities
- resource_limits: validated finite positive limits within server-side maxima
- input_artifact_digests: hashes of staged inputs
- deadline: bounded absolute deadline

Reject unknown schema versions, missing fields, oversized inputs, non-finite numbers, invalid identifiers, duplicate task IDs, and capability requests outside the configured allowlist. Treat model-generated arguments and worker output as untrusted data.

## Execution state machine

RECEIVED -> VALIDATED -> AUTHORIZED -> DISPATCHED -> RUNNING -> COMPLETED -> VERIFIED

Terminal alternatives: REJECTED, DENIED, TIMED_OUT, CANCELLED, WORKER_FAILED, VERIFICATION_FAILED, or UNKNOWN. Never translate a missing runner, absent job, missing artifact, or transport timeout into success.

Transitions must be append-only and correlated. Retries must be idempotent and must not run a non-idempotent task twice merely because the caller lost the response.

## Security invariants

1. Default deny. No execution before a positive control-plane decision.
2. No agent-controlled URL, host path, Docker socket, privileged container, host-root shell, or secret-bearing child-process environment.
3. Use a disposable VM or equivalent strong isolation for untrusted code. A Docker container alone must not be described as a VM.
4. Network egress is denied by default; explicitly allowlist required destinations and enforce policy outside the process as well as in application validation.
5. Credentials are scoped, short-lived where possible, redacted from logs, and never returned to model context.
6. Resource limits and deadlines are enforced by the runner, not merely requested by the agent.
7. Worker output is treated as untrusted. Verify artifact digests and expected schemas before accepting results.
8. Do not register a self-hosted Actions runner or route untrusted pull-request code to it until isolation, one-job lifecycle, token handling, cleanup, and host privilege boundaries have been independently tested.

## Evidence receipt

The receipt should include schema_version, correlation_id, task_id, source repository and SHA, envelope digest, authorization decision and policy version, worker identity, start/end timestamps, exit status, timeout/cancellation status, artifact names and SHA-256 digests, test command identifiers, verifier version, and final outcome.

The receipt proves what the system recorded and verified; it does not independently prove that the worker was honest. Sign receipts with a key unavailable to the worker and protect ledger append operations from worker credentials.

## Acceptance tests

1. Allowed task: returns a correlated receipt and digest-verified artifact.
2. Denied task: worker is never invoked; denial is recorded.
3. Capability escalation: rejected before dispatch.
4. Invalid or oversized envelope: rejected without creating a worker.
5. Worker timeout: process/VM is terminated and outcome is TIMED_OUT, never COMPLETED.
6. Worker crash: outcome is WORKER_FAILED and cleanup is confirmed.
7. Missing runner or no assigned job: outcome is UNKNOWN/INFRASTRUCTURE_BLOCKED, not a test failure or pass.
8. Duplicate task ID: idempotent replay returns the existing outcome without duplicate execution.
9. Artifact mutation: digest verification fails and the result is quarantined.
10. Secret canary: credentials do not appear in worker environment beyond explicit allowlisting, logs, receipts, or model-visible output.
11. Receipt tampering: signature/hash verification fails.
12. Cross-tenant request: authorization denies access and does not disclose artifact metadata.

## Verification reporting

Report each gate separately: source/static checks, focused unit tests, full repository suite, integration tests, VM/worker lifecycle, security tests, and artifact verification. Include exact commit SHA, actual command, exit code, and artifact references. Empty Actions step lists or runner_id=0 mean the test did not execute; they are not a source-code test result.

## Promotion gate

Keep related pull requests draft and unmerged until the focused tests and full supported Python matrix actually execute, coverage artifacts are generated, the isolated worker lifecycle is observed, and a correlated end-to-end receipt is independently verified. Do not trigger an automatic Ollama-driven push. Ollama may prepare a patch, but a separate deterministic gate must validate the diff, run required tests, create an auditable commit, and require explicit operator approval before pushing or merging.

## Implementation order

1. Freeze envelope and receipt schemas.
2. Implement pure validators and state-machine transition tests in AVRONE.
3. Connect the fixed-route Grok Bot bridge with bounded request/response sizes and timeouts.
4. Add a mock runner for deterministic contract tests only; label these tests as mocks.
5. Implement the real isolated runner adapter and verify cleanup/lifecycle on a disposable VM.
6. Connect the authoritative control-plane policy decision.
7. Add artifact hashing and receipt verification.
8. Run the end-to-end acceptance suite in a real environment and publish the evidence bundle.
9. Only then enable the controlled commit/push daemon, with least-privilege GitHub credentials and a human approval gate.