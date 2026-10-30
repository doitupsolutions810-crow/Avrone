# Cross-repository integration findings and release gates

Audit date: 2026-10-30. Scope: AVRONE, Grok Bot, capability-authority,
capability-plane, living-intermediate-control-plane, swarm-vm-runner,
attested-history, and related agent/knowledge repositories.

## Architectural synthesis

The strongest coherent design is a control/evidence/execution separation:

1. **Grok Bot** is the user-facing LLM and agent orchestration surface. Its
   README documents Ollama, MCP, team/squad execution, replayable run logs,
   and an AVRONE bridge. Model output remains untrusted input.
2. **AVRONE** owns the canonical task envelope, lifecycle, bounded orchestration,
   and normalized result. It must not mint privilege or directly expose host
   shell / Docker authority to the model.
3. **capability-authority** is the best current candidate for the authoritative
   policy/capability service: signed short-lived capabilities, Ed25519
   signatures, SPIFFE workload identity binding, capability verification,
   execution receipts, and evidence endpoints.
4. **swarm-vm-runner** is the worker/coordinator candidate, but its current
   optional Actions runner is not an approved untrusted-code boundary.
5. **attested-history** and AttestPipe/evidence components provide the evidence
   direction: append-only linked records, hashes, capability IDs, and verifier
   outcomes. Hash chains alone do not authenticate writers.
6. **living-intermediate-control-plane / capability-plane** contain overlapping
   control-plane and runtime implementations. Select one authority for each
   environment; do not create two independently authoritative policy engines.

## Material findings

### A. Existing interfaces are not yet one integrated execution path

The capability-authority service exposes POST routes:
- `/v1/capabilities/issue`
- `/v1/capabilities/attenuate`
- `/v1/capabilities/verify`
- `/v1/execute`
- `/v1/agents/mount`, `/v1/agents/spawn`, `/v1/agents/list`

It also exposes GET `/health`, `/v1/identity`, and `/v1/evidence`.
Its current executor produces a signed execution receipt for a capability
right. This is a strong reusable base, but it does not itself launch an isolated
VM job or verify the bytes of a worker-produced artifact.

### B. Policy rights need a server-side allowlist before model-facing integration

The inspected `capability-authority/plane_service/policy.py` checks caller
SPIFFE URI prefixes and TTL. It does not, in the inspected implementation,
enforce an action/resource allowlist. The issue handler accepts requested
rights after identity binding. Therefore AVRONE must not forward arbitrary
model-selected rights to `/v1/capabilities/issue`. Add a server-owned mapping
from the three supported task actions to fixed rights, and enforce the same
mapping inside the authority service before issuing any capability.

### C. Host Docker socket was a critical runner boundary

The swarm-vm-runner Compose runner previously mounted
`/var/run/docker.sock`. A workflow could use that socket to control the host
Docker daemon. I removed that mount and the unused runner `GITHUB_TOKEN`
environment injection in commit `c45a7f9f1bacb92fae9de9c11412d587e71a4178`,
then documented the limitation in
`9bd1cfb75a223aa1e04dcf052b6d30a35f65895b`.

This is a mitigation, not a complete VM isolation solution. The runner profile
must not be attached to untrusted pull-request workflows. Jobs requiring
Docker host access are intentionally unsupported by this runner configuration.

### D. Ollama should not own GitHub push authority

Ollama may propose patches and produce a candidate diff. A separate
deterministic gate must inspect changed paths, enforce policy, run the full
test matrix, generate signed evidence, and require operator approval before
pushing or merging. Do not place a broad GitHub token in the model process or
worker environment.

## AVRONE vertical-slice contract

The new `avrone/vertical_slice.py` is an adapter seam, not a deployed bridge.
It takes explicit async dependencies for authorization, isolated execution,
and artifact verification. It fails closed when authorization or execution
fails and refuses to return VERIFIED unless the independent verifier returns
exactly true.

The required production wiring is:

```text
Grok Bot chat
  -> authenticated AVRONE API
  -> strict task envelope + server-owned identity/action mapping
  -> capability-authority (SPIFFE-bound, allowlisted signed capability)
  -> disposable VM runner (one task, bounded CPU/RAM/time/output/network)
  -> artifact staging + independent SHA-256 recomputation
  -> signed receipt + append-only evidence ledger
  -> normalized result to Grok Bot
```

The task envelope's tenant/principal values must come from authenticated
server context, not the model. A timeout or lost response must be reconciled
against an idempotency key before retrying; never dispatch a duplicate
non-idempotent task merely because a caller did not receive the first response.

## Required release gates

- [ ] Add and test action-to-rights allowlist in capability-authority.
- [ ] Connect AVRONE authorization adapter using fixed server-side rights.
- [ ] Validate capability signature, issuer, audience, expiry, policy version,
      and exact requested resource before dispatch.
- [ ] Implement VM lifecycle adapter with no host Docker socket, no ambient
      secrets, default-deny network egress, one-job teardown, and measured
      resource limits.
- [ ] Independent verifier reads staged artifact bytes and recomputes every
      digest; runner-provided booleans are not evidence.
- [ ] Sign the final receipt with a key unavailable to the runner and append
      it to the evidence ledger.
- [ ] Prove idempotency, cancellation, timeout, crash cleanup, cross-tenant
      denial, and secret-canary behavior.
- [ ] Run focused tests, full Python 3.11/3.12 suites, integration tests, and
      VM lifecycle tests. Record commands, exit codes, run IDs, and artifacts.
- [ ] Keep PR #9 draft and unmerged until these gates pass.

## Environment execution limitation

The current execution environment has Python 3.13.5 and Git, but no Docker,
Podman, or QEMU executable was found. A direct Git clone also failed because
the environment cannot resolve github.com. GitHub connector access was used
to inspect and commit source changes, but no local checkout, VM boot, or
pytest execution was possible in this session. Do not report these as passed.

## Profound systems finding

The emerging system's most important unifying invariant is **authority must
decrease as execution moves outward, while evidence must increase**:

- model intent has no authority;
- AVRONE converts intent into a bounded, typed task;
- the control plane grants only a short-lived, attenuated capability;
- the worker receives only the capability and resources needed for one task;
- the verifier independently checks outputs;
- the ledger records the result with cryptographic provenance.

In compact form, for successive stages (i):
`Authority(i+1) <= Authority(i)` and `Evidence(i+1) >= Evidence(i)`.

This is a design invariant, not a measured theorem. It gives the project one
testable criterion for deciding whether a new tool, agent, repository, or
automation improves the system or silently expands its trust boundary.
