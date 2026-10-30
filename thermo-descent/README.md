# Thermo-Descent: The Full Analysis

Branch: `thermo-descent` (from `main` @ b1b00269)

## The Arc

1. **Closed-form gate** — optimal threshold = (gen0 - drain) / (kg*gen0 + 2*kd). Algebraic, not tuned. But it fails at low gen0 because debt is a distribution, not a point.

2. **Debt distribution** — at es=1.0, debt is a perfect fixed point at 2.00 (zero variance). At es=0.0, p10 = 2.00 in every config: drain is the ground state. Distribution-aware gates (mean + k*sd) all clamp to 1.00 and do nothing.

3. **Relaxation linearization** — tau = -log(0.01)/log|J|, J = 1 - kg*gen0/cap - 2*kd/cap. Shock-independent in linear regime (CV 0.248). ANOMALY: gen0=2.0 gives 10x slowdown (68-71 cycles) because the restoring force vanishes at the zero-equilibrium and the max(0,.) floor hard-stops negative excursions.

4. **Bias floor failure** — attempted mitigation gen = max(bias, gen0*(1-kg*d)) had ZERO effect across bias 0.0..1.0. The floor never activates; the slowdown is structural, not a bug.

5. **Self-metering invariant** — failed-evolve debt never exceeds the idle fraction that spawned it. Safety property, not performance. efail/idle ratio caps at 0.5.

6. **Negative net optimum** — gen0=1.5 (below drain 2.0) doubles land rate: 0.744 vs 0.372. The gate's 25% throttle is the primary throughput killer, and it only fires on overshoot. Optimal policy: generate just below drain.

## The Unifying Equation

Three findings, one equation:

**throughput is maximized when generation does not exceed drain.**

- Closed-form gate threshold zero-crosses at gen0 = drain.
- Self-metering invariant bounds debt by idle fraction.
- Negative-net test proves sub-drain generation doubles throughput.

The gate, the invariant, and the optimum are the same statement from three angles.

## What Survives

- kg >= 0.5 hard floor (collapse corner unreachable)
- es as explicit throughput dial
- kd as sole stability knob
- evolve-on-idle (converts spare capacity, self-metering prevents runaway)
- two-stage thermo gate (land 0.883 under stress, phase-flat)
- adaptive wake threshold k=1.5 (wake rate 6.2%, inside 2-8% band)
- sigmoid throttle (prevents collapse, lives in limit cycle period 363)
- stochastic resonance noise 0.10 (debt -60%, headroom not throughput)

## What Died

- Fixed 0.7 gate (replaced by closed-form, which itself is mostly unreachable)
- Distribution-aware k-gates (clamp to 1.00, zero trips)
- Bias floor (zero effect)
- Damping (amplitude unchanged 0.075-0.080)
- Drain modulation (supercrit never below 0.92)
- Inverse gating (collapsed to 0.54)

## Files

- `01_closed_form_gate.py` — threshold derivation + verification
- `02_debt_distribution.py` — distribution analysis + aware-gate test
- `03_relaxation_linearization.py` — tau formula + gen0=2.0 anomaly
- `04_bias_floor_failure.py` — failed mitigation
- `05_self_metering_invariant.py` — efail/idle bound
- `06_negative_net_optimum.py` — sub-drain throughput proof
