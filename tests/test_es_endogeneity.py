from harmonic.es_endogeneity import (
    es_step, es_trajectory, land_rate_from_es, invariant_check, ES_FLOOR, ES_CEIL
)


def test_positive_drift_saturates():
    traj = es_trajectory(0.85, 0.02, cycles=30)
    assert traj[-1] >= 0.99
    assert max(traj) <= ES_CEIL


def test_negative_drift_stalls_at_floor():
    traj = es_trajectory(0.85, -0.02, cycles=40)
    assert traj[-1] == ES_FLOOR
    assert min(traj) >= ES_FLOOR


def test_es_bounded():
    assert es_step(1.5, 0.5) == ES_CEIL
    assert es_step(-0.5, -0.5) == ES_FLOOR


def test_land_rate_linear():
    assert abs(land_rate_from_es(ES_FLOOR) - 0.300) < 1e-9
    assert abs(land_rate_from_es(ES_CEIL) - 0.596) < 1e-9
    mid = land_rate_from_es(0.75)
    assert 0.300 < mid < 0.596


def test_zero_point_five_invariant():
    # 0.5 = idle-fraction budget = efail cap = kg normalization center = ES floor
    assert invariant_check(0.85, 0.5, 0.5, 0.5)
    assert invariant_check(ES_FLOOR, 0.5, 0.5, 0.5)
