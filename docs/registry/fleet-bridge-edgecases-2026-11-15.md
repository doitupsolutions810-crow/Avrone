# AVRONE Fleet boundary evidence register — 2026-11-15

Target: [PR #26](https://github.com/doitupsolutions810-crow/Avrone/pull/26)
Branch: integration/swarm-edge-2026-11-10
Base before this commit: c6305191cbf2deb49142618cea787237c335eb0e

| Resource | Evidence | Status |
| --- | --- | --- |
| [Fleet implementation](../../avrone/fleet.py) | Strict bounded JSON relay size, scan/cycle guards, NaN/Infinity/Unicode isolation, finite health telemetry | Source changes in this PR |
| [21 new regression cases](../../tests/test_bridge_fleet_relay_caps.py) | Malformed relay isolation, exact byte boundary, oversize and cyclic records, non-iterable list subclass, poisoned health, 300 seeded batches | Source committed; hosted tests pending |
| [Bridge fast workflow](../../.github/workflows/bridge-fast.yml) | Python 3.11/3.12 suite includes tests/test_bridge_*.py | Workflow configuration verified |
| [Full CI workflow](../../.github/workflows/ci.yml) | Full-history Python 3.11/3.12 test matrix | Workflow configuration verified |
| [Earlier evidence registry](bridge-edgecase-evidence-2026-11-15.md) | Prior hostile metadata hardening details | GitHub record |
| [Exact-head workflow status](https://github.com/doitupsolutions810-crow/Avrone/pull/26/checks) | Required before promoting this change | Pending |

Local evidence: 21/21 tests passed against a **standalone reproduction of the edited helpers** using Python 3.13.5, including 300 seeded stress batches. A local full-repository checkout could not be obtained because DNS resolution for github.com failed. These are **not** verified full-repository or Python 3.11/3.12 results.

Release: HOLD. No merge, deployment, actual fleet agents, remote execution, or full crash-restart verification was performed in this round.
