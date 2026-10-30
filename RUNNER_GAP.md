# RUNNER_GAP

## What blocks PR 9

Every CI matrix job on this branch fails before its first step with `runner_id: 0`, empty runner name, and empty steps. Python 3.11 fails, Python 3.12 is cancelled. Five consecutive runs show the identical signature. This is not a test failure — no test has ever executed.

## Root cause

The account-level GitHub Actions toggle is disabled. No self-hosted or GitHub-hosted runner can be assigned, so the workflow graph resolves to zero runners and aborts.

## Mitigation path

1. **swarm-vm-runner/runner_registration.py** (emergent-compile, commit bd33a640) — fail-closed module that verifies a minted runner token against the ledger chain, registers it via `gh`, and labels the runner group. Five fail-closed paths: missing ledger is HOLD_UNMEASURED, corrupt chain is HOLD_POISONED, absent token is HOLD_UNLINEAGED, valid token passes, gh-not-installed is HOLD_UNREGISTERED. Self-tested VERIFIED at chain depth two.

2. **scripts/mac-local-squad.sh** (this branch) — launches the Apple Silicon + Ollama squad. Once a real runner exists, this launcher and the registration module feed each other: the launcher boots the environment, the module registers compute into it.

## Success criterion

First CI run after registration shows a nonzero `runner_id`, a populated runner name, and executed steps with coverage.xml and coverage.json artifacts published. Until then this PR stays draft.

## Invariant

A green check with runner_id zero is not green. It is unmeasured. The audit log can no longer be softer than the state it certifies.
