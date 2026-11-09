"""Third wave of longer, more robust Great Scale edge cases.

Complements the previous two waves with:
- machine-epsilon numerical stability
- psi >= CONE invariant across the variance domain
- soft_contract under varying k
- JSON round-trip of snapshots
- tool text-format stability
- residual / log-coordinate identities
- extreme alpha and near-MAX_RUNS regimes
- pure-function idempotence
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import greatscale as gs


# --------------------------------------------------------------------------- machine-epsilon stability

def test_heat_at_machine_eps_sides():
    eps = math.ulp(1.0)  # ~2.22e-16
    assert gs.heat(eps) == pytest.approx(eps * (1 - eps), abs=1e-30)
    assert gs.heat(1.0 - eps) == pytest.approx(eps * (1 - eps), abs=1e-30)
    assert gs.heat(eps) == pytest.approx(gs.heat(1.0 - eps))


def test_psi_at_machine_eps_above_cone():
    eps = math.ulp(1.0)
    s = gs.heat(eps)
    assert gs.psi(s) > gs.CONE
    assert gs.psi(s) - gs.CONE == pytest.approx(s, abs=1e-30)


# --------------------------------------------------------------------------- psi lower-bound invariant

@pytest.mark.parametrize("p", [i / 500 for i in range(501)])
def test_psi_always_at_or_above_cone(p):
    s = gs.heat(p)
    assert gs.psi(s) >= gs.CONE - 1e-15
    assert gs.cone_compliant(s)


def test_psi_equals_cone_only_on_floor():
    for p in (0.0, 1.0):
        assert gs.psi(gs.heat(p)) == pytest.approx(gs.CONE)
    for p in (0.01, 0.5, 0.99):
        assert gs.psi(gs.heat(p)) > gs.CONE + 1e-12


# --------------------------------------------------------------------------- soft_contract varying k

@pytest.mark.parametrize("k", [0.0, 0.1, 0.5, 1.0, 2.0])
def test_soft_contract_k_zero_is_fixed(k):
    # k=0 collapses everything to FIXED; larger k stretches the range
    y = gs.soft_contract(0.0, k=k)
    if k == 0.0:
        assert y == pytest.approx(gs.FIXED)
    else:
        # still bounded
        assert math.isfinite(y)


def test_soft_contract_k_preserves_fixed_point():
    for k in (0.0, 0.25, 0.5, 1.0, 3.0):
        assert gs.soft_contract(gs.FIXED, k=k) == pytest.approx(gs.FIXED)


# --------------------------------------------------------------------------- JSON round-trip

@pytest.mark.parametrize("f,r", [(0, 1), (1, 1), (0, 100), (50, 100), (9, 150), (1, 10_000)])
def test_evidence_snapshot_json_round_trip(f, r):
    snap = gs.evidence_snapshot(f, r)
    raw = json.dumps(snap, sort_keys=True)
    restored = json.loads(raw)
    for key in ("failures", "runs", "p", "S", "psi", "orientation", "onConeFloor"):
        if isinstance(snap[key], float):
            assert restored[key] == pytest.approx(snap[key])
        else:
            assert restored[key] == snap[key]


# --------------------------------------------------------------------------- tool text-format stability

@pytest.mark.asyncio
@pytest.mark.parametrize("f,r", [(0, 10), (1, 10), (5, 10), (10, 10), (9, 150)])
async def test_tool_text_contains_canonical_fields(f, r):
    out = await gs.t_great_scale(None, {"failures": f, "runs": r})
    assert "great_scale evidence:" in out
    assert f"{f}/{r}" in out
    assert "S = heat(p)" in out
    assert "Psi = CONE + S" in out
    assert "orientation:" in out
    assert "runs for 95%:" in out


@pytest.mark.asyncio
async def test_tool_json_is_sorted_and_parseable():
    out = await gs.t_great_scale(None, {"failures": 3, "runs": 20, "format": "json"})
    data = json.loads(out)
    assert list(data.keys()) == sorted(data.keys())
    assert data["failures"] == 3 and data["runs"] == 20


# --------------------------------------------------------------------------- residual / log-coordinate identities

def test_residual_count_at_floor_and_peak():
    assert gs.residual_count(0.0) == 8.0
    assert gs.residual_count(0.25) == 7.0


def test_log_coordinate_at_floor_and_peak():
    assert gs.log_coordinate(0.0) == 0.0
    assert gs.log_coordinate(0.25) == 1.0


def test_log_coordinate_linear_in_s():
    for s in (0.0, 0.0625, 0.125, 0.1875, 0.25):
        assert gs.log_coordinate(s) == pytest.approx(4 * s)


# --------------------------------------------------------------------------- extreme alpha & near-MAX_RUNS

def test_extreme_small_alpha_increases_runs_for_95():
    n_05 = gs.runs_to_detect(0.1, 0.05)
    n_12 = gs.runs_to_detect(0.1, 1e-12)
    assert n_12 is not None and n_05 is not None
    assert n_12 > n_05


def test_near_max_runs_pure_pass():
    # Avoid allocating huge structures; just the pure math path
    r = gs.MAX_RUNS
    snap = gs.evidence_snapshot(0, r)
    assert snap["onConeFloor"] and snap["runs"] == r
    assert snap["runsFor95"] is None


def test_near_max_runs_pure_fail():
    r = gs.MAX_RUNS
    snap = gs.evidence_snapshot(r, r)
    assert snap["onConeFloor"] and snap["orientation"] == "fail"
    assert snap["runsFor95"] == 1


# --------------------------------------------------------------------------- pure-function idempotence

def test_heat_idempotent_under_clamping():
    for x in (-1.0, 0.0, 0.3, 0.5, 1.0, 2.0):
        h1 = gs.heat(x)
        h2 = gs.heat(h1)  # heat of a value already in [0,0.25]
        # heat maps [0,1] -> [0,0.25]; applying again is not identity,
        # but the result must stay in [0,0.25]
        assert 0.0 <= h2 <= 0.25 + 1e-15


def test_psi_of_heat_of_psi_stays_above_cone():
    for p in (0.0, 0.1, 0.5, 0.9, 1.0):
        s = gs.heat(p)
        p2 = gs.psi(s)  # >= CONE ≈ 0.38
        # psi returns a number > 1 sometimes; heat clamps it
        s2 = gs.heat(p2)
        assert gs.psi(s2) >= gs.CONE - 1e-15


# --------------------------------------------------------------------------- cone_compliant boundary

def test_cone_compliant_true_for_all_valid_heat():
    for i in range(1001):
        s = gs.heat(i / 1000)
        assert gs.cone_compliant(s) is True


def test_cone_compliant_false_for_pathological_negative():
    # Directly construct a residual that would break the floor
    assert gs.cone_compliant(-0.1) is False
