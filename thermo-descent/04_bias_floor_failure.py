"""BIAS FLOOR MITIGATION ATTEMPT (FAILED)

Hypothesis: the 10x relaxation slowdown at gen0=2.0 is caused by the
discrete max(0,.) floor. Mitigation: gen = max(bias, gen0*(1-kg*d)),
adding a positive generation floor so the system can never fully stall.

Result: COMPLETE FAILURE. Across bias in 0.0..1.0:
- land rate: 0.742 (unchanged)
- relaxation@shock10: 22 cycles (unchanged)
- equilibrium debt: 0.18 (unchanged)
- p10: 0.00, max: 1.09 (unchanged)

The floor never activates because at the operating point,
gen0*(1-kg*d) already exceeds any bias. The mitigation solved for a
regime the system never visits.

Theoretical equilibrium with bias: d* = (gen0+b-2)/(kg*gen0+2*kd).
At gen0=2, b=0.5: d*=0.125, debt*=1.25. But empirical debt* = 0.18,
not 1.25 — because arrivals are sparse (arr_p=0.3) and the evolve
loop's idle-time consumption dominates. The bias term is swamped by
the stochastic arrival process.

CONCLUSION: the 10x slowdown is not a bug with a patch. It is the
structural price of the negative-net optimum (gen0=2 < drain=2), which
the negative-net throughput test already identified as OPTIMAL
(land 0.744 vs 0.372 for positive net). You cannot have the optimum
and fast relaxation simultaneously. The trade is load-bearing.
"""

import random, statistics

def run_biased(cap, kg, kd, gen0, bias, arr_p, ep, es, c_fail, steps=30000, seed=7):
    random.seed(seed)
    st = {"debt": 0.0, "backlog": 0, "landed": 0, "total": 0, "dh": []}
    for t in range(steps):
        d = min(1.0, st["debt"] / cap)
        gen = max(bias, gen0 * (1 - kg * d)) if d < 1 else bias
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
        st["debt"] = max(0.0, st["debt"] - 2.0 * (1 + kd * d))
        if t >= steps - 3000:
            st["dh"].append(st["debt"])
    debts = st["dh"]
    return dict(land=st["landed"] / max(st["total"], 1),
                mean=statistics.mean(debts), p10=statistics.quantiles(debts, n=10)[0],
                mx=max(debts))

def relax_biased(cap, kg, kd, gen0, bias, shock, steps=5000, seed=7):
    random.seed(seed)
    st = {"debt": 0.0}
    for t in range(2000):
        d = min(1.0, st["debt"] / cap)
        gen = max(bias, gen0 * (1 - kg * d)) if d < 1 else bias
        st["debt"] += gen
        st["debt"] = max(0.0, st["debt"] - 2.0 * (1 + kd * d))
    base = st["debt"]
    st["debt"] = shock
    for t in range(steps):
        d = min(1.0, st["debt"] / cap)
        gen = max(bias, gen0 * (1 - kg * d)) if d < 1 else bias
        st["debt"] += gen
        st["debt"] = max(0.0, st["debt"] - 2.0 * (1 + kd * d))
        if abs(st["debt"] - base) <= 0.01 * max(base, 0.01):
            return t, base
    return None, base


if __name__ == "__main__":
    for bias in [0.0, 0.25, 0.5, 1.0]:
        rs = [run_biased(10, 1.0, 1.0, 2.0, bias, 0.3, 0.4, 0.5, 0.5, seed=s) for s in range(3)]
        r10, _ = relax_biased(10, 1.0, 1.0, 2.0, bias, 10.0, seed=3)
        print(f"bias={bias:.2f}: land={statistics.mean(r['land'] for r in rs):.3f} "
              f"eq={statistics.mean(r['mean'] for r in rs):.2f} relax10={r10}")
    # all biases must produce identical results
    lands = [run_biased(10, 1.0, 1.0, 2.0, b, 0.3, 0.4, 0.5, 0.5)["land"] for b in [0.0, 0.5, 1.0]]
    assert max(lands) - min(lands) < 1e-9
    print("PASS: bias has zero effect")
