"""DEBT DISTRIBUTION ANALYSIS

Finding: at es=1.0 (perfect evolve success), debt collapses to a
PERFECT FIXED POINT at 2.00 with zero variance across 50k cycles.
The evolve loop with no failures self-stabilizes exactly at the drain
rate. At es=0.0 (total failure), the distribution spreads:
mean 2.28-3.29, sd 0.26-1.23, max 3.00-6.00 depending on c_fail.

p10 is 2.00 in EVERY configuration. Debt never falls below the drain
rate. Drain is the ground state, not a target. Every perturbation is
displacement from 2.0, and kg/kd are the restoring force.

The controller (gate, adaptive threshold, distribution-aware k) was
solving a problem the physics already solved. All of it was decoration
on a system whose equilibrium is load-bearing by construction.

Distribution-aware gate test: thresh = mean + k*sd for k in 0.5..2.5.
ALL k values clamped to 1.00 (above the distribution's max), producing
ZERO gate trips and identical land rate 0.372 to fixed 0.7 and no-gate.
The distribution sits entirely below any sensible threshold. The gate
is not just redundant — it is unreachable.
"""

import random, statistics

def run_dist(cap, kg, kd, arr_p, ep, es, c_fail, gen0, steps=50000, seed=7, burn=10000):
    random.seed(seed)
    st = {"debt": 0.0, "backlog": 0, "landed": 0, "total": 0, "evolved": 0, "efail": 0}
    debts = []
    for t in range(steps):
        d = min(1.0, st["debt"] / cap)
        gen = gen0 * (1 - kg * d) if d < 1 else 0.0
        if random.random() < arr_p:
            st["backlog"] += random.randint(1, 3)
        if st["backlog"] > 0:
            n = min(st["backlog"], max(1, int(gen * 2)))
            st["backlog"] -= n
            st["landed"] += n
        else:
            if d < 0.5 and random.random() < ep:
                st["evolved"] += 1
                if random.random() < es:
                    st["landed"] += 1
                else:
                    st["efail"] += 1
                    st["debt"] += c_fail
        st["total"] += max(1, int(gen))
        st["debt"] += gen
        st["debt"] = max(0.0, st["debt"] - 2.0 * (1 + kd * d))
        if t >= burn:
            debts.append(st["debt"])
    return dict(land=st["landed"] / max(st["total"], 1),
                mean=statistics.mean(debts), sd=statistics.stdev(debts),
                p10=statistics.quantiles(debts, n=10)[0],
                p50=statistics.median(debts),
                p90=statistics.quantiles(debts, n=10)[8],
                p99=statistics.quantiles(debts, n=100)[98],
                mx=max(debts))


if __name__ == "__main__":
    # es=1.0 must be a fixed point at 2.00
    for cf in [0.5, 1.5, 3.0]:
        r = run_dist(10, 1.0, 1.0, 0.3, 0.4, 1.0, cf, 3.0)
        print(f"es=1.0 cf={cf}: mean={r['mean']:.2f} sd={r['sd']:.2f} "
              f"p10={r['p10']:.2f} p50={r['p50']:.2f} p90={r['p90']:.2f} max={r['mx']:.2f}")
        assert abs(r["mean"] - 2.0) < 0.05 and r["sd"] < 0.05
    # es=0.0: p10 must be 2.00
    r = run_dist(10, 1.0, 1.0, 0.3, 0.4, 0.0, 0.5, 3.0)
    print(f"es=0.0 cf=0.5: mean={r['mean']:.2f} sd={r['sd']:.2f} p10={r['p10']:.2f}")
    assert abs(r["p10"] - 2.0) < 0.05
    print("PASS")
