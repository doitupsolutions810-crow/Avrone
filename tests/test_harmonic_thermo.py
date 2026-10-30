from harmonic.harmonic_thermo import LaneState, ThermoLedger, CAP, STEEPNESS


def test_energy_guard():
    assert LaneState(0, 0, 0, 0).energy() == 1.0
    assert LaneState(0, 0, 0, 1).energy() == 1.0
    assert abs(LaneState(1, 0, 0, 0).energy() - 1.0) < 1e-9
    assert abs(LaneState(1, 0, 10, 0).energy() - 1 / 11) < 1e-9


def test_kg_governor():
    L = ThermoLedger()
    for _ in range(100):
        L.record_evolve(True)
    assert L.kg() < 1.0
    L2 = ThermoLedger()
    for _ in range(100):
        L2.record_evolve(False)
    assert L2.kg() > 1.0


def test_adaptive_threshold():
    L = ThermoLedger()
    L.register("a")
    L.lanes["a"].stale_ms = 1000
    L.record()
    t1 = L.adaptive_threshold(3.0, 2.0, 1.0)
    L.lanes["a"].stale_ms = 5000
    L.record()
    t2 = L.adaptive_threshold(3.0, 2.0, 1.0)
    assert t2 >= t1


def test_sigmoid_fixed_point():
    import random
    L = ThermoLedger()
    random.seed(0)
    d = 0.5
    for _ in range(200):
        d = 3.0 * L.sigmoid_throttle(d, 0.5) - 2.0 * min(d, 2.0) + random.uniform(-0.05, 0.05)
        d = max(0.0, d)
    tail = []
    for _ in range(50):
        d = 3.0 * L.sigmoid_throttle(d, 0.5) - 2.0 * min(d, 2.0) + random.uniform(-0.05, 0.05)
        d = max(0.0, d)
        tail.append(d)
    assert max(tail) - min(tail) < 0.5


def test_tone_regime():
    L = ThermoLedger()
    L.register("a")
    assert L.tone_for_debt(0.8) == "literal"
    assert L.tone_for_debt(0.1) == "grounded"
    L.lanes["a"].committed = 5
    assert L.regime()["processor"] > L.regime()["evolve"]
