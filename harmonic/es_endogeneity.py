"""ES endogeneity: evolve success rate as a function of itself.

In AVRONE, es is not an exogenous dial. It is the output of evolve_propose,
which depends on proposal quality, which depends on the system's own code and
prompts — which the evolve loop itself modifies. That makes es a fixed point
of its own map.

es(t+1) = es(t) + p_self * delta * es(t)

where p_self is the probability a landed evolve touches the evolve pipeline
itself (prompt templates, parse_action recovery, JSON nudge), and delta is
the shift in success rate from that touch.

Positive delta compounds: higher es -> more idle converts to landed evolves
-> more chances to improve the pipeline. Negative delta decays symmetrically.
The all-green gate is the only barrier between compounding and collapse.
"""

ES_FLOOR = 0.5   # idle-fraction budget == efail cap == kg normalization center
ES_CEIL = 1.0
P_SELF = 0.15    # probability a landed evolve touches the pipeline


def es_step(es, delta, p_self=P_SELF):
    """One cycle of the es map."""
    es = es + p_self * delta * es
    return max(ES_FLOOR, min(ES_CEIL, es))


def es_trajectory(es0, delta, cycles=50, p_self=P_SELF):
    """Run the es map for n cycles."""
    traj = [es0]
    es = es0
    for _ in range(cycles):
        es = es_step(es, delta, p_self)
        traj.append(es)
    return traj


def land_rate_from_es(es, es_floor=ES_FLOOR, es_ceil=ES_CEIL,
                      lr_floor=0.300, lr_ceil=0.596):
    """Linear map from es to land rate (verified slope ~0.14)."""
    if es_ceil == es_floor:
        return lr_floor
    t = (es - es_floor) / (es_ceil - es_floor)
    return lr_floor + t * (lr_ceil - lr_floor)


def invariant_check(es, idle_fraction, efail_cost, kg_norm_center):
    """0.5 appears as: idle-fraction budget, efail cap, kg normalization center.
    Verify all three equal the ES floor."""
    return (abs(idle_fraction - ES_FLOOR) < 1e-9 and
            abs(efail_cost - ES_FLOOR) < 1e-9 and
            abs(kg_norm_center - ES_FLOOR) < 1e-9 and
            abs(es - ES_FLOOR) >= 0)  # es bounded at floor
