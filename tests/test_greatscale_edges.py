"""Longer, more robust edge-case battery for avrone.greatscale.

Complements tests/test_greatscale.py with:
- pooling / Jeffreys registers (Descent III tables)
- soft_contract modular edges
- topological saturation extremes
- type pollution and boundary runs
- monotonicity of runs_to_detect
- orientation under accumulation
"""
from __future__ import annotations

import math

import pytest

from avrone import greatscale as gs


# --------------------------------------------------------------------------- helpers

def jeffreys(f: int, r: int) -> float:
    """Jeffreys-smoothed p used in Descent III tables."""
    return (f + 0.5) / (r + 1)


def pool(*snaps: dict) -> dict:
    """Sum failures and runs across silos; recompute snapshot."""
    f = sum(s["failures"] for s in snaps)
    r = sum(s["runs"] for s in snaps)
    return gs.evidence_snapshot(f, r)


# --------------------------------------------------------------------------- Jeffreys / pooling (Descent III)

def test_jeffreys_2_over_2_is_not_cone_floor():
    # Descent III: 2/2 under Jeffreys is Psi_J ≈ 0.520855, not the floor
    p_j = jeffreys(2, 2)
    s_j = gs.heat(p_j)
    assert p_j == pytest.approx(0.833333, abs=1e-6)
    assert s_j == pytest.approx(0.138889, abs=1e-6)
    assert gs.psi(s_j) == pytest.approx(0.520855, abs=1e-5)
    assert not (s_j == 0.0)


def test_pooled_confirmations_descend_toward_cone():
    # Descent III table: as pooled r grows under all-confirmations, Psi -> CONE
    expected = [
        (1, 0.569466),
        (2, 0.520855),
        (4, 0.471966),
        (10, 0.425354),
        (40, 0.394012),
        (400, 0.383211),
    ]
    for r, psi_j in expected:
        p = jeffreys(r, r)  # all confirmations
        assert gs.psi(gs.heat(p)) == pytest.approx(psi_j, abs=1e-5)


def test_fragmented_vs_pooled_contradiction_alarm():
    # Two silos on opposite cone floors; fragmented heat = 0 everywhere;
    # pooled 2/4 lands at maximal heat S = 0.25
    silo_a = gs.evidence_snapshot(2, 2)  # always fail
    silo_b = gs.evidence_snapshot(0, 2)  # always pass
    assert silo_a["onConeFloor"] and silo_b["onConeFloor"]
    assert silo_a["S"] == silo_b["S"] == 0.0

    pooled = pool(silo_a, silo_b)
    assert pooled["failures"] == 2 and pooled["runs"] == 4
    assert pooled["p"] == 0.5
    assert pooled["S"] == 0.25
    assert pooled["psi"] == pytest.approx(gs.CONE + 0.25)
    assert pooled["orientation"] == "mixed"
    assert not pooled["onConeFloor"]


def test_pooling_is_commutative_and_associative():
    a = gs.evidence_snapshot(1, 10)
    b = gs.evidence_snapshot(3, 20)
    c = gs.evidence_snapshot(0, 5)
    assert pool(a, b, c) == pool(c, a, b) == pool(pool(a, b), c)


# --------------------------------------------------------------------------- soft_contract modular edges

@pytest.mark.parametrize("x", [0, 9, 18, -9, -18, 4.5, 13.5, 8.999, 9.001, 1e6, -1e6])
def test_soft_contract_range_and_fixed_point(x):
    y = gs.soft_contract(x)
    # result always in [FIXED - 0.5*4.5, FIXED + 0.5*4.5] = [2.25, 6.75] for k=0.5
    assert 2.25 - 1e-12 <= y <= 6.75 + 1e-12
    # FIXED is the unique fixed point of the map when m = FIXED
    assert gs.soft_contract(gs.FIXED) == pytest.approx(gs.FIXED)


def test_soft_contract_periodicity_mod_9():
    for base in range(-20, 30):
        assert gs.soft_contract(base) == pytest.approx(gs.soft_contract(base + 9))


# --------------------------------------------------------------------------- topological saturation extremes

@pytest.mark.parametrize("tau", [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 10.0])
def test_from_topological_saturation_always_cone_ok(tau):
    out = gs.from_topological_saturation(tau)
    assert out["coneOk"] is True
    assert out["S"] == pytest.approx((1 - tau * tau) / 4)
    # S can be negative when |tau| > 1; heat is only defined on [0,1],
    # but the topological map is allowed to report negative residual
    assert out["psi"] == pytest.approx(gs.CONE + out["S"])


def test_topological_saturation_at_unit_tau_is_zero_residual():
    out = gs.from_topological_saturation(1.0)
    assert out["S"] == 0.0
    assert out["psi"] == pytest.approx(gs.CONE)


# --------------------------------------------------------------------------- type pollution & boundaries

@pytest.mark.parametrize("bad", [
    None, "10", 1.5, complex(1, 0), [], {}, object(), float("nan"), float("inf"),
])
def test_evidence_snapshot_rejects_non_int_counts(bad):
    with pytest.raises((ValueError, TypeError)):
        gs.evidence_snapshot(bad, 10)  # type: ignore[arg-type]
    with pytest.raises((ValueError, TypeError)):
        gs.evidence_snapshot(0, bad)  # type: ignore[arg-type]


def test_evidence_snapshot_accepts_exact_max_runs():
    snap = gs.evidence_snapshot(0, gs.MAX_RUNS)
    assert snap["runs"] == gs.MAX_RUNS
    assert snap["onConeFloor"]


def test_evidence_snapshot_rejects_above_max_runs():
    with pytest.raises(ValueError):
        gs.evidence_snapshot(0, gs.MAX_RUNS + 1)


def test_heat_clamps_outside_unit_interval():
    assert gs.heat(-0.5) == 0.0
    assert gs.heat(1.5) == 0.0
    assert gs.heat(0.0) == 0.0
    assert gs.heat(1.0) == 0.0


def test_heat_near_half_is_peak():
    assert gs.heat(0.5) == 0.25
    assert gs.heat(0.5 - 1e-15) < 0.25
    assert gs.heat(0.5 + 1e-15) < 0.25


# --------------------------------------------------------------------------- runs_to_detect monotonicity & alpha edges

def test_runs_to_detect_monotone_in_p():
    # higher defect rate => fewer runs needed
    prev = gs.runs_to_detect(0.001)
    for p in (0.01, 0.05, 0.1, 0.2, 0.5, 0.9, 0.99):
        n = gs.runs_to_detect(p)
        assert n is not None and n <= prev
        prev = n

@pytest.mark.parametrize("alpha", [1e-6, 0.01, 0.05, 0.1, 0.5, 0.99])
def test_runs_to_detect_respects_alpha(alpha):
    p = 0.1
    n = gs.runs_to_detect(p, alpha)
    assert n is not None
    assert 1 - (1 - p) ** n >= 1 - alpha - 1e-12
    if n > 1:
        assert 1 - (1 - p) ** (n - 1) < 1 - alpha


def test_runs_to_detect_rejects_alpha_boundaries():
    with pytest.raises(ValueError):
        gs.runs_to_detect(0.5, 0.0)
    with pytest.raises(ValueError):
        gs.runs_to_detect(0.5, 1.0)
    with pytest.raises(ValueError):
        gs.runs_to_detect(0.5, -0.1)


# --------------------------------------------------------------------------- orientation under accumulation

def test_orientation_stable_under_pure_confirmations():
    for r in (1, 2, 10, 100):
        assert gs.evidence_snapshot(0, r)["orientation"] == "pass"
        assert gs.evidence_snapshot(r, r)["orientation"] == "fail"


def test_orientation_flips_to_mixed_on_first_disagreement():
    assert gs.evidence_snapshot(0, 10)["orientation"] == "pass"
    # one failure among ten
    assert gs.evidence_snapshot(1, 10)["orientation"] == "mixed"
    # nine failures among ten
    assert gs.evidence_snapshot(9, 10)["orientation"] == "mixed"


# --------------------------------------------------------------------------- cascade / residual identities

def test_residual_count_and_cascade_lambda_identities():
    for s in (0.0, 0.0625, 0.25, 0.5):
        n = gs.residual_count(s)
        assert n == pytest.approx(8 - 4 * s)
        assert gs.cascade_lambda(n) == pytest.approx(gs.PHI ** (-n))


def test_log_coordinate_bounds():
    for p in (i / 1000 for i in range(1001)):
        lc = gs.log_coordinate(gs.heat(p))
        assert 0.0 <= lc <= 1.0 + 1e-15


# --------------------------------------------------------------------------- tool surface under edge args (async)

@pytest.mark.asyncio
async def test_tool_alpha_override_changes_runs_for_95():
    out_default = await gs.t_great_scale(None, {"failures": 1, "runs": 20, "format": "json"})
    out_strict = await gs.t_great_scale(None, {"failures": 1, "runs": 20, "alpha": 0.01, "format": "json"})
    d = __import__("json").loads(out_default)
    s = __import__("json").loads(out_strict)
    assert s["runsFor95"] >= d["runsFor95"]  # stricter alpha needs more runs


@pytest.mark.asyncio
async def test_tool_h_mode_rejects_bool_and_nan():
    for bad in (True, False, float("nan"), float("inf"), -0.1, 1.1):
        out = await gs.t_great_scale(None, {"h": bad})
        assert out.startswith("ERROR")
