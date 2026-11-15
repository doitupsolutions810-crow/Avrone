# AVRONE Fleet relay merge accounting — extended edge-case evidence
Date: 2026-11-15
Repository: https://github.com/doitupsolutions810-crow/Avrone
PR: https://github.com/doitupsolutions810-crow/Avrone/pull/26
Branch: integration/swarm-edge-2026-11-10
Pre-change head: 7c10f9ad7ea8b38d15af2c72e9a4fc33233340a4

## Verified defects before patch
1. bridge_merge returned len(posts) even when merge_remote reported merged=0 and skipped>0. This inflated merge success accounting for duplicates and rejected records.
2. sanitize_health clamped active_agents by passing a raw int subclass to min/max, which called overridden __lt__/__gt__. A reproducer raised RuntimeError instead of returning sanitized telemetry.

## Changes and validation registry

| Source | Evidence | Certification |
| --- | --- | --- |
| [Fleet source](../../avrone/fleet.py) | Counts actual admitted events, clamps incorrect results, safely extracts native int for active_agents | GitHub source changed |
| [Fleet merge accounting regressions](../../tests/test_bridge_fleet_merge_accounting.py) | 29 repository test cases (27 isolated helper cases plus 2 real Bridge integration cases), 500 seeded stress scenarios, duplicates/rejections with real Bridge | Test module committed; exact-head CI pending |
| [Previous Fleet evidence](fleet-bridge-edgecases-2026-11-15.md) | Relay 64 KiB and hostile telemetry limits | Prior committed evidence |
| [Bridge fast workflow](../../.github/workflows/bridge-fast.yml) | Includes tests/test_bridge_*.py for Python 3.11 and 3.12 | Workflow inspected |
| [Full CI workflow](../../.github/workflows/ci.yml) | Full checkout, Python 3.11/3.12 pytest | Workflow inspected |
| [PR checks](https://github.com/doitupsolutions810-crow/Avrone/pull/26/checks) | Exact-head verification required before merge | Pending |

An **isolated local helper reproduction**, not the full repository, passed 48/48 tests including earlier 21 relay cap tests and the new 27 accounting tests. It also reproduced both pre-fix defects. This does not certify the checkout, Python 3.11/3.12 matrices, distributed agents, crash testing, or deployment.

**Release decision: HOLD.** No merge/deployment is authorized by this report.
