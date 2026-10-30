# AVRONE adapter wiring: control plane and artifact verification

Status date: 2026-10-30

## Implemented interfaces

- `avrone.capability_authority.authorize_with_capability_authority`
  issues a server-mapped task right and verifies the Ed25519 capability signature
  using `AVRONE_PLANE_PUBLIC_KEY_B64`, which must be provisioned out-of-band.
- `avrone.artifact_verifier.verify_staged_artifacts_async`
  reopens staged artifact bytes, rejects traversal and symlinks, enforces the
  task output-byte budget, and recomputes SHA-256.
- `avrone.vertical_slice.execute_vertical_slice`
  carries the complete authorization decision into the worker request under
  `_authorization`; malformed worker digests and missing worker identities
  cannot produce a VERIFIED receipt.

The authority adapter supports only the enumerated task actions:
`run_tests`, `build_artifact`, and `analyze_repository`. It maps these to
fixed server-defined rights. The authority service independently enforces its
allowlist. The public key is not trusted from the HTTP response.

## Required configuration

Provision these values through a trusted secret/configuration mechanism, not
through model context or a worker-controlled environment:

- `AVRONE_CAPABILITY_AUTHORITY_URL`: private authority endpoint.
- `AVRONE_PLANE_PUBLIC_KEY_B64`: pinned raw Ed25519 public key, base64 encoded.
- `AVRONE_PLANE_ISSUER`: expected broker SPIFFE URI (default
  `spiffe://prod/broker`).
- `AVRONE_PLANE_TRUST_DOMAIN`: expected trust domain (default `prod`).
- `AVRONE_PLANE_AUDIENCE`: exact AVRONE SPIFFE workload identity; required and compared to the signed capability audience.
- `AVRONE_ARTIFACT_ROOT`: dedicated, per-task staging directory mounted
  read-only to the verifier and writable only by the worker during execution.

Example composition at the application boundary:

```python
from avrone.capability_authority import authorize_with_capability_authority
from avrone.artifact_verifier import verify_staged_artifacts_async
from avrone.vertical_slice import execute_vertical_slice

result = await execute_vertical_slice(
    authenticated_task_envelope,
    allowed_capabilities={"read_repository"},
    authorize=authorize_with_capability_authority,
    run_isolated=provision_and_run_one_disposable_vm,  # REQUIRED adapter; not yet present
    verify_artifacts=verify_staged_artifacts_async,
)
```

`provision_and_run_one_disposable_vm` is intentionally shown as a required
integration point, not a supplied implementation. It must create one disposable
VM per task, mount only the task input and output staging areas, enforce CPU,
memory, wall-clock, and output limits, deny network egress by default, provide
no host Docker socket or ambient GitHub credentials, collect a worker identity,
and destroy the VM on success, failure, timeout, and cancellation.

## Why the existing swarm coordinator is not the VM adapter

The inspected `swarm-vm-runner/coordinator/server.js` exposes a generic queue
and claim/complete API. It does not itself provision a VM, enforce per-job
resource/network isolation, bind an issued capability to a VM process, or
guarantee destruction after a task. Its current runner profile is therefore
not a substitute for the required disposable VM adapter.

## Test and release evidence

The AVRONE workflow now includes task-contract, execution-state, vertical-slice,
capability-adapter, and artifact-verifier tests. GitHub created workflow runs,
but the observed jobs had `runner_id: 0` and no executed steps. Those are
runner-allocation failures, not test results. A passing local or self-hosted
execution is still required.

The capability-authority local CI script now includes allowlist and TTL checks
in `tests/test_plane.py`.

Do not merge the AVRONE PR or connect this path to untrusted pull requests until
the disposable VM adapter, cleanup tests, and actual CI execution are complete.


## Claude Agent SDK guest worker contract

The optional SDK worker is packaged in `avrone/claude_agent_node.py`. A
provider-backed guest image with the `claude-agent` extra installed runs:

```bash
python -m avrone.claude_agent_node
```

The VM backend sends exactly one UTF-8 JSON object on stdin with these fields:
`task_id`, `correlation_id`, `objective`, `source_repository`, and
`source_commit_sha`. The payload is capped at 24 KiB and rejects extra keys.
The backend injects the following guest environment values from its own
provisioning record (never from model output): `AVRONE_VM_LEASE_ID`,
`AVRONE_VM_WORKER_ID`, `AVRONE_VM_WORKSPACE`, `AVRONE_VM_DISPOSABLE=1`,
`AVRONE_VM_NETWORK_EGRESS_DENIED=1`, `AVRONE_VM_DOCKER_SOCKET_MOUNTED=0`,
and `AVRONE_VM_GITHUB_CREDENTIALS_MOUNTED=0`. Missing or malformed security
flags fail closed.

The process emits one JSON line on stdout and exits non-zero on failure. The
backend must bound stdin/stdout, wall time, CPU, memory and disk; kill the guest
on timeout; collect workspace files only after completion; independently hash
the bytes; and destroy the VM regardless of outcome. These environment flags
are an adapter contract, not a provider attestation or proof of isolation.
Only the actual provider's control-plane evidence can establish those controls.


## AVRONE VMBackend binding

`avrone.claude_vm_backend.ClaudeSDKVMBackend` implements AVRONE's existing
`VMBackend` protocol and can be passed directly to
`DisposableVMRunner(ClaudeSDKVMBackend(provider, objective_resolver=...))`.
The objective resolver must read the full objective from authenticated
server-side task storage; the strict task envelope deliberately does not let
a model inject arbitrary extra fields.

The provider interface must provision a new `ClaudeVMLease`, execute the
guest command `python -m avrone.claude_agent_node` with the adapter's bounded
stdin and non-secret lease environment, collect staged artifact bytes, and
destroy the VM. It must independently enforce the advertised isolation;
the adapter's flags do not themselves prove isolation. Provider credentials
for the model must be injected into the guest from a secret manager or
workload identity, never from the task, prompt, or assistant output.

The adapter validates the guest result's worker and lease IDs before
collection, requires collection identity to match, and preserves the SDK
summary separately from artifact digests. The existing vertical slice then
reopens the collected artifact bytes, verifies SHA-256, and only then creates
a receipt. Teardown remains in `DisposableVMRunner`'s finally path.


### Composition example

```python
from avrone.claude_vm_backend import ClaudeSDKVMBackend
from avrone.disposable_vm_runner import DisposableVMRunner
from avrone.integrated_execution import execute_with_capability_authority

# provider is an implementation supplied by the VM infrastructure team;
# objective_store reads the authenticated, server-owned task description.
vm_runner = DisposableVMRunner(
    ClaudeSDKVMBackend(
        provider,
        objective_resolver=objective_store.get_for_task,
    )
)
result = await execute_with_capability_authority(
    validated_task_envelope,
    allowed_capabilities={"read_repository"},
    run_isolated=vm_runner,
    authority_url=capability_authority_url,
    artifact_root=task_artifact_root,
    receipt_signer_url=receipt_signer_url,
)
```

This composes the existing AVRONE control-plane path directly:
validate task envelope → verify signed capability → provision disposable VM
→ execute Claude SDK guest worker → collect artifacts → independently verify
artifact bytes → request signed receipt → destroy VM. The injected
`provider` is the sole infrastructure-specific piece still outstanding.
