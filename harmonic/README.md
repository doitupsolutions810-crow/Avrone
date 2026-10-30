# harmonic_thermo - AVRONE thermodynamic core

- LaneState entropy: stale_ms*0.01 + uncommitted_bytes + c_fail*rejected
- Energy = committed / (committed + entropy); 1.0 if total < 1e-12
- kg governor: 1.5 - evolve_yield (clamped, floor 0.5)
- Adaptive gate threshold = equilibrium + rolling noise, clamped 0.9
- Sigmoid steepness = 1.0 (bifurcation fix; period-2 eliminated)
- Primary readouts: es (evolve success), net flow (gen0 - drain), regime fractions
- Debt oscillation treated as cosmetic unless crossing cap
- Invariants: efail <= idle_fraction, gen0 > drain, threshold = d_star + noise
