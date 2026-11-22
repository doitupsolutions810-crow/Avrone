# Evolve Residual Wire

## What changed
- `avrone/evolve.py` `_attempt_row` now calls `evidence_snapshot`-equivalent math
  (failures/runs -> S, orientation, cone status) before recording any attempt.
- Unmeasured outcomes (zero runs) are tagged HOLD_UNMEASURED — they can never
  authorize a proposal acceptance.
- `evolve_loop` gains a saturation stop axis: when the last three residuals are
  identical within float noise, the proposal space is saturated and further
  iterations only re-measure the same heat.

## Why
Evolve was the last subsystem treating verification as a boolean. Every other
layer (CONTROL12, Living Intermediate, Great Scale, the adapter) is residual-aware.
This wire closes that gap on the system's self-modification path.

## Success criteria
1. Every attempt row carries a residual field. [wire]
2. HOLD_UNMEASURED blocks authorization. [wire]
3. Saturation stop fires on flat residual trend. [wire]
4. Flaky-retry greens still carry the flaky flag alongside S=0 orientation=pass. [parity]
