"""CLOSED-FORM GATE THRESHOLD

Finding: the optimal gate threshold is not a tuned constant.
It is the algebraic point where net flow crosses zero:

    threshold = (gen0 - drain) / (kg * gen0 + 2 * kd)

where gen0 is base generation, drain is the fixed drain rate (2.0),
kg is the generation-depression coefficient, kd is the drain-amplification
coefficient.

Verification across 13 configs (gen0 in 1.5..5.0, kg/kd in 0.25..3.0,
c_fail in 0.5..3.0, arrival in 0.3..0.8):

- At gen0=3.0, kg=1, kd=1: threshold = 0.20. Land rate 0.391 vs 0.372
  for fixed 0.7. Gate trips dropped from ~10k to ~2.2k. Mean debt fell
  2.15 -> 1.75.
- At gen0=2.0 (always-negative net): closed form clamps to 0.0 and LOSES
  0.350 land rate vs fixed 0.7. The 0.7 brake catches something the
  algebra says shouldn't exist at low generation.
- At gen0=5.0: loses 0.019. Formula right about the crossover, wrong
  about where the system actually lives.

Conclusion: debt is a DISTRIBUTION, not a point mass. The evolve loop's
failure injections (c_fail per failed evolve) push the distribution's center
away from the algebraic crossover. A point-threshold gate is structurally
misaligned with a stochastic process.
"""

import random, statistics

def run_closed(cap, kg, kd, arr_p, ep, es, c_fail, gen0, steps=20000, seed=7):
    drain = 2.0
    gt = max(0.0, min(1.0, (gen0 - drain) / (kg * gen0 + 2 * kd)))
    random.seed(seed)
    st = {"debt": 0.0, "backlog": 0, "landed": 0, "total": 0, "gate_trips": 0, "dh": []}
    for t in range(steps):
        d = min(1.0, st["debt"] / cap)
        gen = gen0 * (1 - kg * d) if d < 1 else 0.0
        if d > gt:
            gen *= 0.25
            st["gate_trips"] += 1
        if random.random() < arr_p:
            st["backlog"] += random.randint(1, 3)
        if st["backlog"] > 0:
            n = min(st["backlog"], max(1, int(gen * 2)))
            st["backlog"] -= n
            st["landed"] += n
        else:
            if d < 0.5 and random.random() < ep:
                if random.random() < es:
                    st["landed"] += 1
                else:
                    st["debt"] += c_fail
        st["total"] += max(1, int(gen))
        st["debt"] += gen
        st["debt"] = max(0.0, st["debt"] - drain * (1 + kd * d))
        if t >= steps - 2000:
            st["dh"].append(st["debt"])
    return dict(land=st["landed"] / max(st["total"], 1),
                gate_trips=st["gate_trips"],
                mean_debt=statistics.mean(st["dh"]), gt=gt)


if __name__ == "__main__":
    # sanity: gen0=3, kg=1, kd=1 -> threshold 0.20
    r = run_closed(10, 1.0, 1.0, 0.3, 0.4, 0.5, 0.5, 3.0)
    print(f"gt={r['gt']:.2f} land={r['land']:.3f} trips={r['gate_trips']} debt={r['mean_debt']:.2f}")
    assert abs(r["gt"] - 0.20) < 1e-9
    print("PASS")
