# ES Endogeneity - The Fixed Point of Self-Improvement

## The finding

es is not a dial. It is a fixed point of its own map:

    es(t+1) = es(t) + p_self * delta * es(t)

- **Positive delta** (delta = +0.02): es climbs 0.85 -> 1.0 in ~8 cycles, locks at ceiling. Land rate saturates at 0.596. Generation slope stops mattering — the evolve loop carries everything.
- **Negative delta** (delta = -0.02): es decays 0.85 -> 0.5 in ~20 cycles, stalls at the ES floor. Land rate slides to 0.300.

## The 0.5 invariant

The constant 0.5 appears at three layers:
1. **Idle-fraction budget** — failed-evolve debt can never exceed the idle fraction that spawned it.
2. **ES floor** — evolve success rate cannot fall below 0.5 in the model.
3. **kg normalization center** — the evolve-yield governor's linear map is centered at 0.5 (neutral yield).

In each case, 0.5 marks the boundary between the system being able to absorb its own mistakes and not. It is not a tuned parameter. It is the same boundary read from three angles.

## The mitigation

- Stop treating 0.5 as a constant. Derive it from runtime idle fraction.
- Promote es to a live ledger sensor (logged per evolve cycle alongside restart-cost).
- The adaptive wake threshold fires on es collapse, not just entropy spikes.
- Regime fractions (processor / evolve / idle) replace raw debt as the dashboard's primary input.

## Success criteria

- es trajectory bounded in [0.5, 1.0] across all delta in [-0.05, 0.05]
- Positive drift reaches ceiling within 15 cycles
- Negative drift stalls at floor, never below
- land_rate(es) linear with slope ~0.14, no cliffs
- 0.5 invariant holds: idle budget == efail cap == kg center == ES floor
