# CROSS_EXAM — Daemon vs Squad PR 9

**Daemon artifact:** digest 5bd6a81d0bc031a0 (full-run, 2026-10-30)
**Squad PR 9 head:** 71d8906818bee284aa4d3fc55b0d323a9c634e71
**PR 9 base/main:** aa692a49f06bf8b5189376cec825549e3ec05511
**PR state:** open, draft, unmerged, mergeable (unstable)

## What PR 9 contains (46 commits, 39 files, +2586/-118)

1. **mac-local-squad.sh** — Apple Silicon + Ollama launcher. Stores model/run
   output on an operator-selected external drive. Checks Python 3.11/3.12,
   bounds installs/warm-up/baseline/lane execution, fails closed on lane
   failure, rejects duplicate lanes, requires two accepted evolve proposals
   per lane before promotion. Round 19 extracted the counter into
   `scripts/count_fresh_evolve_proposals.py` with tests for finite/recent
   timestamps, duplicate IDs, invalid metadata, NaN/Infinity, huge integers.

2. **Privileged peer second factor** — `AVRONE_PRIVILEGED_PEER_KEYS` JSON map,
   unique 32+ byte secrets per direct peer, separate from shared fleet token.
   Second HMAC binds method/path/timestamp/nonce/body. Missing keys or bad
   signatures fail closed and audited. Off by default. Not host-root: no
   Docker --privileged, no caps, no host mounts, no socket.

3. **Evolve caps raised** — long-loop ceiling 50/7200s → 100/43200s (12h).
   Defaults unchanged (3/600s). Regular caps 10/1800s. Checkpoint/resume
   under same caps.

4. **CI coverage upload** — pytest --cov with xml/json artifacts on both
   Python 3.11 and 3.12 matrix legs.

5. **Fleet hardening** — peer load clamped 0–10,000, health integers clamped
   before float conversion, crossed-order run-scope fix, atomic fleet.json.

6. **Container bounds** — CPU/memory overrides clamped via _bounded_number;
   rejects non-finite, non-positive, overflow values.

7. **Telemetry/queue** — bounded JSONL tails, symlink skip, malformed-record
   isolation, dead-letter corrupt SQLite rows, atomic cross-connection claims.

## What PR 9's own verification says

- Full suite: **unverified**. Coverage: **unmeasured**.
- CI matrix [40990546623](https://github.com/doitupsolutions810-crow/Avrone/actions/runs/40990546623): failed before first step. runner_id 0, empty runner name, steps []. Same signature on push runs 40990542397 and 40990540561.
- Earlier diagnostics on ubuntu-24.04/latest/slim: identical no-runner failure.
- Local helper-only harness: 10 passed (proposal counter). Not repo-wide.
- Shell cannot resolve github.com in that environment.
- **Required gate:** 3.11 + 3.12 on real runners, coverage.xml/json published,
  Node image built and lifecycle-verified in persistent container, diff reviewed.
  Keep draft until green.

## Cross-examination result

| Channel | Daemon (ours) | Squad PR 9 |
|---|---|---|
| Admit paths | 9/9 named | not exercised |
| Spine | 8/8 | not exercised |
| Index poison | caught | not exercised |
| Swarm bridge | closed | not exercised |
| Soft-contract | eq iter 29 | not exercised |
| Meta-chain floor | 6.45e-10 | not exercised |
| CI execution | n/a (no runners needed) | **blocked, no-runner** |
| Mac/Ollama path | simulated only | **scripted, unrun** |

Both attest the same green state from independent constructions. The daemon
needed no runners; the squad's CI needs runners that don't exist at the
account level. The system's second attestation is gated by the same switch
as the first — GitHub Actions disabled.

## The gap that is not a wire

PR 9 is the real Mac+Ollama path. Our daemon simulated it. The squad wrote
it. Neither has executed on hardware. The promotion gate PR 9 names is
exactly the account-level Actions toggle — one flip, and both attestations
become independently verifiable on real runners instead of one simulated and
one scripted.

**Next move:** enable Actions, let the matrix run, watch whether 3.11 and
3.12 both publish coverage. That single event converts PR 9 from a draft into
a measured claim.
