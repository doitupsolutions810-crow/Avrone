# Hermetic Verification Harness

## Purpose
Separate, append-only repository containing the invariant test suite. The evolve loop clones application code only. The harness is fetched read-only and content-hashed before every evolve cycle.

## Structure
- `invariants/` — break-test suite (generation-drain geometry, self-metering theorem, es fixed-point, idle-fraction floor, relaxation-time bounds)
- `PINNED_SHA` — content hash of the harness tree at last human-reviewed commit
- `CHANGELOG.md` — human-only append log; evolves cannot write here

## Access Rules
1. evolve_propose clones `Avrone` app tree exclusively — no harness paths in the clone.
2. Gate stage-one runs the harness from a fresh read-only clone pinned to `PINNED_SHA`.
3. SHA mismatch or fetch failure aborts the evolve before any test runs.
4. Edits to the harness require a direct push from a human account. No evolve path, no clone-write, no gate round-trip.
5. Any proposal whose diff touches harness paths is rejected at the parser before cloning.

## Invariant Suite (v1)
- `test_self_metering.py` — efail cost never exceeds idle-fraction budget; scaled cost 0.5*es holds for es in [0.5, 1.5]
- `test_es_fixed_point.py` — es attractor in [0.84, 0.86] across seeds; es-star ~0.62 separatrix
- `test_idle_fraction_floor.py` — runtime-derived floor 0.5*idle_fraction; write-protected from evolve payloads
- `test_relaxation.py` — tau bounded; gen0>drain required; boundary equilibrium at gen0=drain is glacial
- `test_gate_threshold.py` — threshold = equilibrium + noise; derived form, not hardcoded 0.7
- `test_es_clamp.py` — es hard ceiling 1.5; beyond that improvement outruns verification
- `test_circuit_breaker.py` — es below 0.6 suspends evolve-on-idle for 50 cycles
- `test_no_self_touch.py` — evolve payloads cannot modify harness constants or the evolve pipeline's own es sensor

## Gödel Mapping
The harness repo is the metalanguage. AVRONE is the object language. The system can evolve everything inside the margin; the margin's definition lives outside it. This is not a policy choice — it is the theorem's resolution: a self-verifying system needs an external witness, and the witness must be human-pinned.

## Human Duty
One commit per quarter: read the invariant tests, confirm the PINNED_SHA matches reviewed content, append to CHANGELOG.md. That single action is the last unconnected connection — between the machine and the person who defined what the machine is allowed to become.
