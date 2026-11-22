# SECRETS_ROTATION

Public history across the six core repos exposed credential patterns. This file ranks them by blast radius and gives the rotation order. The audit log can no longer be softer than the state it certifies — so the state stops depending on anything the log revealed.

## Rank 1 — Immediate (account-wide)

- **TrainLLM dev-secret default** (`auth.ts`, `/token` route). Any token minted under the default signs `scale-sla` with a symmetric key shipped in source. Rotate: set `TRAINLLM_JWT_SECRET` (or ed25519 keypair) in every environment, delete the default branch, invalidate all outstanding tokens. The `/token` route is now gated behind `authorize` — verify no tokens were minted before the gate landed.
- **GitHub Actions disabled at account level.** Thirty-four repos, 2300+ tests, zero CI heartbeats. Not a secret, but the absence is the exposure: nothing has been verifying pushes since the branches diverged. Re-enable, then bind `swarm-vm-runner` ephemeral runners so the first green is real.

## Rank 2 — Per-repo, before any deploy

- **capability-plane / capability-authority SPIFFE/SPIRE node keys.** If these were ever generated with default or empty config, the attestpipe bus's signed receipts are forgeable. Rotate node identities, re-attest the mesh.
- **swarm-vm-runner `mint-runner-token.sh`.** Fail-closed by design, but the token it mints is the key to the job fabric. Rotate after any clone of that script left the perimeter.
- **control12-workspace connector proofs** (artifacts/). These are live attestation artifacts sitting in a public repo. Move to attested-history (private) and re-anchor.

## Rank 3 — Hygiene

- **great-scale Vercel deployment** (`great-scale.vercel.app`). Public frontend reading residual math is fine; confirm no env-backed secrets are exposed through it.
- **living-intermediate-control-plane Vercel app** (`avrone-due-krey-chat.vercel.app`). Same check.
- **grok-bot- public frontend.** Browser service + public UI; audit for any key material in client bundles.

## Invariant

No secret that appears in git history is trusted after this date. Rotation is not optional for Rank 1 — it is the HOLD_UNLINEAGED gate applied to the account's own past.
