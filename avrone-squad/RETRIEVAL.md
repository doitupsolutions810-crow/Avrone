# AVRONE Sandbox Persistence Mitigation

## Problem
The code_execution sandbox does not persist files between calls. Every
new invocation starts from a clean /tmp, so artifacts drafted in one
turn vanish before the next.

## Solution: self-contained retrieval bundle
This directory in the Avrone repo IS the durable copy. Everything
drafted in sandbox sessions lives here on GitHub:

  1. harmonic/harmonic_dash.py      - proposal D (thermo gauge spec + D3 snippet + injector)
  2. tests/test_harmonic_dash.py    - 5 offline tests
  3. tests/test_harmonic_smoke.py   - 2 offline smoke tests (loopback server)
  4. dashboard/README.md            - dashboard docs
  5. evolve-payloads/proposal_E.json- ready-to-fire evolve_propose payload
  6. evolve-payloads/MANIFEST.json  - full A-E file map + cherry-pick order + tarball SHA
  7. DAILY_LOG.md                   - session log
  8. RETRIEVAL.md                   - this file

## How to restore anywhere
  git clone https://github.com/doitupsolutions810-crow/Avrone.git
  cd Avrone/avrone-squad
  python -m pytest tests/ -q

## How to re-derive from scratch
Every file is also reconstructible from the conversation that produced
it. The two deltas from first drafts: injector guards on the quoted
id (not bare), and smoke-test server roots at the temp dir.

## Integrity
Tarball SHA-256: 0f7e210463bff5cc6ad67fbd399bfea94856fa33033e45b20bc22d576dfc467f
(avrone-squad-artifacts.tar.gz, 5224 bytes, from the final sandbox session)
