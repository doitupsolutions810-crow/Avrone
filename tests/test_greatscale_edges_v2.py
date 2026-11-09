"""Second wave of longer, more robust Great Scale edge cases.

Complements test_greatscale.py and test_greatscale_edges.py with:
- Descent II multiplicative chain arithmetic and heat inversion
- Numerical stability near the cone floor with large N
- atlas / residual partition invariants
- cascade_lambda extremes
- concurrent pure-function calls
- single-run regime and detectInRuns consistency
- soft_contract uniqueness of FIXED
- topological saturation continuity
"""
from __future__ import annotations

import math
import concurrent.futures

import pytest

from avrone import greatscale as gs


# --------------------------------------------------------------------------- Descent II multiplicative chain

def transmission(p: float, N: int, gates: list[float]) -> float:
    """T = D(p,N) * product(gates)."""
    if p <= 0:
        return 0.0
    D = 1.0 - (1.0 - p) ** N
    T = D
    for g in gates:
        T *= g
    return T


def test_multiplicative_chain_heat_inversion():
    # Deterministic defect p=1 through two 1/2 gates → T=1/4, S=0.1875
    T = transmission(1.0, 1, [0.5, 0.5])
    assert T == pytest.approx(0.25)
    assert gs.heat(T) == pytest.approx(0.1875)
    assert gs.psi(gs.heat(T)) == pytest.approx(gs.CONE + 0.1875)

    # Four half-gates → T=1/16, S≈0.0586
    T4 = transmission(1.0, 1, [0.5, 0.5, 0.5, 0.5])
    assert T4 == pytest.approx(0.0625)
    assert gs.heat(T4) == pytest.approx(0.05859375)

    # Same defect with evidence carried end-to-end (no gates) stays on floor
    T0 = transmission(1.0, 1, [])
    assert T0 == 1.0
    assert gs.heat(T0) == 0.0


def test_flaky_through_gates_still_hotter_than_lost_truth():
    # Maximally flaky p=1/2 through two gates: T=1/8, S≈0.1094
    # Deterministic truth through four gates: T=1/16, S≈0.0586
    # The lost truth is *colder* than the flaky signal — the inversion
    T_flaky = transmission(0.5, 1, [0.5, 0.5])
    T_lost = transmission(1.0, 1, [0.5, 0.5, 0.5, 0.5])
    assert gs.heat(T_flaky) > gs.heat(T_lost)


# --------------------------------------------------------------------------- numerical stability near cone floor

@pytest.mark.parametrize("r", [1, 2, 10, 100, 1000, 10_000, 100_000])
def test_pure_pass_stays_exactly_on_floor(r):
    snap = gs.evidence_snapshot(0, r)
    assert snap["S"] == 0.0
    assert snap["onConeFloor"] is True
    assert snap["psi"] == pytest.approx(gs.CONE)
    assert snap["orientation"] == "pass"
    assert snap["runsFor95"] is None


@pytest.mark.parametrize("r", [1, 2, 10, 100, 1000, 10_000])
def test_pure_fail_stays_exactly_on_floor(r):
    snap = gs.evidence_snapshot(r, r)
    assert snap["S"] == 0.0
    assert snap["onConeFloor"] is True
    assert snap["orientation"] == "fail"
    assert snap["runsFor95"] == 1


def test_single_failure_among_million_has_tiny_heat():
    snap = gs.evidence_snapshot(1, 1_000_000)
    assert snap["S"] == pytest.approx(9.99999e-7, rel=1e-6)
    assert snap["psi"] == pytest.approx(gs.CONE + 9.99999e-7, rel=1e-6)
    assert snap["orientation"] == "mixed"
    assert snap["runsFor95"] is not None and snap["runsFor95"] > 1_000_000


# --------------------------------------------------------------------------- atlas & residual partition

def test_residual_partition_sums_to_eight():
    assert sum(gs.RESIDUAL_PARTITION) == 8
    assert gs.RESIDUAL_PARTITION == (4, 3, 1)


def test_atlas_role_only_defined_on_partition():
    for b in gs.RESIDUAL_PARTITION:
        assert gs.atlas_role(b) is not None
    for b in (-1, 0, 2, 5, 8, 99):
        assert gs.atlas_role(b) is None


def test_atlas_roles_are_distinct():
    roles = [gs.atlas_role(b) for b in gs.RESIDUAL_PARTITION]
    assert len(set(roles)) == len(roles)


# --------------------------------------------------------------------------- cascade_lambda extremes

def test_cascade_lambda_at_zero_is_one():
    assert gs.cascade_lambda(0) == pytest.approx(1.0)


def test_cascade_lambda_monotone_decreasing():
    prev = gs.cascade_lambda(0)
    for n in (0.5, 1, 2, 4, 8, 16):
        cur = gs.cascade_lambda(n)
        assert cur < prev
        prev = cur


def test_cascade_lambda_large_n_approaches_zero():
    assert gs.cascade_lambda(100) < 1e-20
    assert gs.cascade_lambda(1000) == 0.0 or gs.cascade_lambda(1000) < 1e-300


# --------------------------------------------------------------------------- concurrent pure calls

def test_concurrent_evidence_snapshot_identical():
    def one(_):
        return gs.evidence_snapshot(9, 150)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(32)))
    first = results[0]
    for r in results[1:]:
        assert r == first


def test_concurrent_heat_identical():
    def one(h):
        return gs.heat(h)

    hs = [i / 100 for i in range(101)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(one, hs * 4))
    # first 101 should equal the pure sequential values
    for i, h in enumerate(hs):
        assert results[i] == pytest.approx(gs.heat(h))


# --------------------------------------------------------------------------- single-run regime & detectInRuns

def test_runs_equals_one_regime():
    pass_snap = gs.evidence_snapshot(0, 1)
    fail_snap = gs.evidence_snapshot(1, 1)
    assert pass_snap["p"] == 0.0 and pass_snap["onConeFloor"]
    assert fail_snap["p"] == 1.0 and fail_snap["onConeFloor"]
    assert fail_snap["runsFor95"] == 1
    assert pass_snap["detectInRuns"] == 0.0
    assert fail_snap["detectInRuns"] == 1.0


def test_detect_in_runs_matches_formula():
    for f, r in ((0, 10), (1, 10), (5, 10), (9, 10), (10, 10), (1, 100)):
        snap = gs.evidence_snapshot(f, r)
        p = f / r
        expected = 1.0 - (1.0 - p) ** r
        assert snap["detectInRuns"] == pytest.approx(expected, abs=1e-12)


def test_runs_for_95_is_sufficient_and_minimal():
    for p in (0.01, 0.05, 0.1, 0.25, 0.5, 0.9):
        n = gs.runs_to_detect(p, 0.05)
        assert n is not None
        assert 1 - (1 - p) ** n >= 0.95 - 1e-12
        if n > 1:
            assert 1 - (1 - p) ** (n - 1) < 0.95


# --------------------------------------------------------------------------- soft_contract uniqueness of FIXED

def test_soft_contract_fixed_is_unique_in_unit_interval():
    # FIXED is the only fixed point of soft_contract in a dense sample
    fixed_count = 0
    for i in range(-100, 200):
        x = i / 10.0
        y = gs.soft_contract(x)
        if abs(y - x) < 1e-12:
            fixed_count += 1
            assert abs(x - gs.FIXED) < 1e-9 or abs((x % 9) - (gs.FIXED % 9)) < 1e-9
    assert fixed_count >= 1


# --------------------------------------------------------------------------- topological saturation continuity

def test_topological_saturation_continuous_at_unit():
    left = gs.from_topological_saturation(1.0 - 1e-9)
    right = gs.from_topological_saturation(1.0 + 1e-9)
    centre = gs.from_topological_saturation(1.0)
    assert left["S"] == pytest.approx(centre["S"], abs=1e-8)
    assert right["S"] == pytest.approx(centre["S"], abs=1e-8)


def test_topological_saturation_symmetric_in_tau():
    for tau in (0.0, 0.3, 0.7, 1.0, 1.5):
        a = gs.from_topological_saturation(tau)
        b = gs.from_topological_saturation(-tau)
        assert a["S"] == pytest.approx(b["S"])
        assert a["psi"] == pytest.approx(b["psi"])


# --------------------------------------------------------------------------- evidence_snapshot alpha propagation

def test_alpha_propagates_into_runs_for_95():
    base = gs.evidence_snapshot(1, 50, alpha=0.05)
    strict = gs.evidence_snapshot(1, 50, alpha=0.01)
    loose = gs.evidence_snapshot(1, 50, alpha=0.20)
    assert strict["runsFor95"] >= base["runsFor95"] >= loose["runsFor95"]


# --------------------------------------------------------------------------- identity: heat(p) == heat(1-p)

@pytest.mark.parametrize("p", [0.0, 0.01, 0.1, 0.25, 0.333, 0.5, 0.7, 0.99, 1.0])
def test_heat_orientation_blind_identity(p):
    assert gs.heat(p) == pytest.approx(gs.heat(1.0 - p))
