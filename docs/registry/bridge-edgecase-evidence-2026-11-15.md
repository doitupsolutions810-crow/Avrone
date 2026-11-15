# AVRONE Bridge extended edge-case evidence registry
Date: 2026-11-15
Target: doitupsolutions810-crow/Avrone, PR #26
Branch: integration/swarm-edge-2026-11-10
Code/test evidence head before registry: fda0123043a64b8665929fc4d6438c6434664ff3
Release decision: HOLD (draft, unmerged, undeployed)

## Source-to-claim register

| Source | Verifiable claim | Evidence status |
| --- | --- | --- |
| [PR #26](https://github.com/doitupsolutions810-crow/Avrone/pull/26) | Branch and draft status, head SHA, commits and diff metadata | GitHub metadata observed |
| [Bridge source](https://github.com/doitupsolutions810-crow/Avrone/blob/fda0123043a64b8665929fc4d6438c6434664ff3/avrone/bridge.py) | Stringifier failures are contained during normalized metadata ingestion, local kind validation and remote batch processing; malformed ID equality is not invoked; nested hostile integer projection falls back to zero; timestamp comparator catches TypeError | Code committed, not yet run through latest complete CI |
| [New regression module](https://github.com/doitupsolutions810-crow/Avrone/blob/fda0123043a64b8665929fc4d6438c6434664ff3/tests/test_bridge_hostile_metadata_ingestion.py) | Six parametrized hostile metadata cases, isolated invalid remote batch case, bad local kind, poisoned timestamp, hostile ID equality, nested integer projection, and a seeded loop of 512 metadata combinations | 12 pytest cases defined; 512 loop iterations defined; execution pending |
| [Bridge fast workflow](https://github.com/doitupsolutions810-crow/Avrone/blob/fda0123043a64b8665929fc4d6438c6434664ff3/.github/workflows/bridge-fast.yml) | Runs tests/test_bridge_*.py on Python 3.11 and 3.12, so the new test file is included | Workflow selection verified |
| [Full CI workflow](https://github.com/doitupsolutions810-crow/Avrone/blob/fda0123043a64b8665929fc4d6438c6434664ff3/.github/workflows/ci.yml) | Full-history checkout and pytest -q on Python 3.11 and 3.12 | Configuration verified; latest head not yet certified |
| [Fast workflow run](https://github.com/doitupsolutions810-crow/Avrone/actions/runs/43754159832) | Run observed queued on code/test head fda0123043a64b8665929fc4d6438c6434664ff3 | Queued at inspection, no pass conclusion |
| [Full CI run](https://github.com/doitupsolutions810-crow/Avrone/actions/runs/43754159808) | Run observed queued on code/test head fda0123043a64b8665929fc4d6438c6434664ff3 | Queued at inspection, no pass conclusion |

## This hardening round's Git commits

1. [850add0](https://github.com/doitupsolutions810-crow/Avrone/commit/850add081b34cafb7a5ce341bd9f2ae1bcfa28a8): contain hostile metadata / comparator conversion failures.
2. [8cb1d2c](https://github.com/doitupsolutions810-crow/Avrone/commit/8cb1d2cb732e5894cdb19e6bfdd8d1c4ed533d9e): add conversion isolation and 512 seeded metadata inputs.
3. [910d59b](https://github.com/doitupsolutions810-crow/Avrone/commit/910d59b584f58e3e114ec397b7133fe84980e4b7): reject hostile post IDs without invoking custom equality and guard integer projection.
4. [fda0123](https://github.com/doitupsolutions810-crow/Avrone/commit/fda0123043a64b8665929fc4d6438c6434664ff3): add the two matching targeted regressions.

## Validation limits and next requirements

- This environment has no connected Desktop Commander device and its local sandbox cannot clone GitHub repositories. Do not label the new tests or latest full suite as locally passing.
- New workflow runs may remain queued; only conclusions on the exact current head, for both Python versions, can clear test validation.
- Pass results on older commits are historical evidence only; they do not certify this head.
- No merge, deployment, external provider execution, or end-to-end distributed swarm attestation occurred in this round.
- Promotion gate remains HOLD until both latest matrices pass, followed by actual Runtime HTTP/bridge and crash-restart/atomic filesystem verification.
