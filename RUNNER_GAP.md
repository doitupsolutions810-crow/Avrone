# CI Runner Gap — PR #9

## Observed state

The latest inspected CI matrix has two jobs (Python 3.11 and 3.12). The failing jobs report `runner_id: 0`, an empty runner name, and no executed steps; the run produces no coverage artifacts. This proves that GitHub did not assign a runner to those jobs. It does **not**, by itself, identify why the runner was not assigned.

The account-level Actions-disable explanation is currently **unverified**. Possible causes include repository/account/organization Actions policy, billing or usage restrictions, runner-group access, hosted-runner availability, or workflow configuration. An owner/admin must inspect the Actions settings and billing state before naming a root cause.

## A self-hosted runner would not match the current CI job yet

Avrone's `.github/workflows/ci.yml` currently says:

```yaml
runs-on: ubuntu-24.04
strategy:
  matrix: { python: ["3.11", "3.12"] }
```

The candidate `swarm-vm-runner` image advertises labels `self-hosted, linux, x64, control12, swarm`. A runner carrying those labels cannot satisfy a job that requests `ubuntu-24.04`. Registering it alone does not unblock PR #9. Moving the matrix to self-hosted labels would be a separate workflow change; the two matrix jobs need two simultaneously available runner slots (or an explicitly serialized matrix).

Do not switch the workflow to self-hosted until the runner host is isolated for untrusted workflow code. The current swarm compose runner mounts `/var/run/docker.sock` into the runner container. A workflow with access to that socket can control the host Docker daemon; this is a high-privilege trust boundary. Avrone's `pull_request` workflow must not route untrusted fork code to a runner with host-level access.

## Cross-repository audit: `swarm-vm-runner`

Inspected branch `emergent-compile`, commit `bd33a6405123e223d959a804cb3d262e98c8ad7a`.

The registration module in that branch is **not yet a working registration path**:

1. `runner_registration.py` asks for `ledger/success_proofs.jsonl`, but that file is absent from the branch. The actual ledger directory contains `HEAD`, `SUCCESS-2026-09-17.json`, and `INTERNAL-RUNNER-2026-09-17.json`, with a different `prev` / `sha256` / nested-`payload` structure.
2. `scripts/mint-runner-token.sh` obtains a GitHub repository registration token and optionally prints it; it does not append a matching token hash to `success_proofs.jsonl`. Therefore the module's token-lineage lookup has no producer in the inspected branch.
3. `register_runner()` invokes `gh runner add`. The documented GitHub registration sequence configures the downloaded Actions runner application with `./config.sh --url <repository-url> --token <registration-token>`; GitHub says the registration token expires after one hour. See [GitHub: adding self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners) and [the self-hosted runners REST API](https://docs.github.com/en/rest/actions/self-hosted-runners).
4. `self_test()` defines a temporary-ledger wrapper but does not install it as the global verifier before calling `register_runner()`. Its last assertion therefore does not test the advertised `gh`-missing path against that temporary ledger. The self-test constructs one valid entry (depth one), not a depth-two chain.
5. The actual `runner/entrypoint.sh` does call `./config.sh` correctly, but the Compose runner service has `restart: unless-stopped` while configuring an `--ephemeral` runner from an environment-supplied token. A container restart can attempt to reuse a token that has already been consumed or has expired; a production launcher needs to mint a fresh token for each new ephemeral runner process.

Accordingly, the registration module should be treated as a draft, not a verified mitigation. The existing Compose runner path also needs a lifecycle and security review before use with PR workflows.

## Required next actions

1. A repository/account owner checks Actions permissions and billing/usage restrictions in GitHub Settings. Record the exact setting or GitHub error; do not infer it from `runner_id: 0`.
2. Re-run the unchanged hosted CI workflow. A real assignment must show a nonzero runner ID, a runner name, and executed steps.
3. If a self-hosted path is selected, first harden its token lifecycle and isolate the runner host; then add an explicit self-hosted CI workflow using matching labels and provision two workers for the Python matrix.
4. Run Python 3.11 and 3.12 tests and publish both `coverage.xml` and `coverage.json`.
5. Keep PR #9 draft and unmerged until those gates pass. The optional Node image and persistent-agent service lifecycle remain a separate verification gate.

## Invariant

A workflow status without an assigned runner and executed steps is **unmeasured**, not a test pass. Do not promote based on a green-looking summary that contains no executed work.
