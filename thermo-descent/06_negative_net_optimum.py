"""NEGATIVE NET OPTIMUM

Finding: at net flow = gen0 - drain:
- net +1.0 (gen 3, drain 2): land rate 0.372
- net  0.0 (gen 2, drain 2): land rate 0.372 (identical)
- net -0.5 (gen 1.5, drain 2): land rate 0.744 (DOUBLED)

Negative net outperforms positive net. Reason: lower generation means
lower debt, means the 0.7 gate threshold never triggers, means the
25% throttle never engages. The gate is not a backstop — it is the
PRIMARY THROUGHPUT KILLER, and it only fires because generation
overshoots.

The system was designed as if more generation was always better. The
data says the opposite: optimal policy is to generate just below drain.

This is the same equation as the closed-form gate threshold at its
zero-crossing, and the same invariant as self-metering. Three findings,
one equation: throughput is maximized when generation does not exceed
drain, because the gate's only job is to punish overshoot, and the
punishment is worse than the crime.
"""

import random, statistics

def run_net(gen0, drain=2.0, cap=10, kg=1.0, kd=1.0, arr_p=0.3, ep=0.4, es=0.5,
            c_fail=0.5, steps=20000, seed=7, gate=0.7):
    random.seed(seed)
    st = {"debt": 0.0, "backlog": 0, "landed": 0, "total": 0, "dh": []}
    for t in range(steps):
        d = min(1.0, st["debt"] / cap)
        gen = gen0 * (1 - kg * d) if d < 1 else 0.0
        if d > gate:
            gen *= 0.25
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
    return st["landed"] / max(st["total"], 1), statistics.mean(st["dh"])


if __name__ == "__main__":
    for gen0, label in [(3.0, "net+1"), (2.0, "net0"), (1.5, "net-0.5")]:
        lands = [run_net(gen0, seed=s)[0] for s in range(5)]
        print(f"gen0={gen0:.1f} ({label}): land={statistics.mean(lands):.3f} "
              f"across seeds {min(lands):.3f}-{max(lands):.3f}")
    # net-0.5 must beat net+1
    l_pos = statistics.mean(run_net(3.0, seed=s)[0] for s in range(5))
    l_neg = statistics.mean(run_net(1.5, seed=s)[0] for s in range(5))
    assert l_neg > l_pos
    print(f"PASS: negative net ({l_neg:.3f}) beats positive net ({l_pos:.3f})")
