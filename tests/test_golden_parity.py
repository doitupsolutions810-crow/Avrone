"""Golden-vector parity: Avrone's Python Great Scale vs great-scale/public/residual.cjs.

Single source of truth: golden_vectors.json. Any language port that diverges
fails here. This is wire one of the emergent-compile mitigation.
"""
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from avrone import greatscale as gs

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "golden_vectors.json").read_text())
FIXTURE_CJS = Path(__file__).parent / "fixtures" / "great_scale_residual.cjs"
NODE = shutil.which("node")


def test_meta_constants():
    assert VECTORS["meta"]["cone"] == pytest.approx(gs.CONE, abs=1e-15)
    assert VECTORS["meta"]["phi"] == pytest.approx(gs.PHI, abs=1e-15)
    assert VECTORS["meta"]["fixed_point"] == gs.FIXED
    assert tuple(VECTORS["meta"]["residual_partition"]) == gs.RESIDUAL_PARTITION


def test_heat_vectors_match_python():
    for row in VECTORS["heat"]:
        assert gs.heat(row["h"]) == pytest.approx(row["S"], abs=1e-12)


def test_psi_vectors_match_python():
    for row in VECTORS["psi"]:
        assert gs.psi(row["S"]) == pytest.approx(row["psi"], abs=1e-12)


def test_cascade_lambda_vectors():
    for row in VECTORS["cascade_lambda"]:
        assert gs.cascade_lambda(row["n"]) == pytest.approx(row["lambda"], rel=1e-12)


def test_topological_saturation_vectors():
    for row in VECTORS["topological_saturation"]:
        out = gs.from_topological_saturation(row["tau"])
        for k in ("S", "n", "lambda", "logCoord", "psi"):
            assert out[k] == pytest.approx(row[k], rel=1e-12, abs=1e-15)
        assert out["coneOk"] is row["coneOk"]


def test_soft_contract_vectors():
    for row in VECTORS["soft_contract"]:
        assert gs.soft_contract(row["x"]) == pytest.approx(row["out"], abs=1e-12)


def test_residual_count_vectors():
    for row in VECTORS["residual_count"]:
        assert gs.residual_count(row["S"]) == pytest.approx(row["n"], abs=1e-12)


def test_evidence_channel_vectors():
    for row in VECTORS["evidence_channel"]:
        snap = gs.evidence_snapshot(row["failures"], row["runs"])
        assert snap["S"] == pytest.approx(row["S"], abs=1e-12)
        assert snap["orientation"] == row["orientation"]
        assert snap["onConeFloor"] is row["onConeFloor"]
        if row["runsFor95"] is None:
            assert snap["runsFor95"] is None
        else:
            assert snap["runsFor95"] == row["runsFor95"]


def test_soft_contract_fixed_point_is_center_of_ninefold():
    assert gs.soft_contract(4.5, 0.5) == 4.5
    assert gs.soft_contract(4.5, 0.0) == 4.5
    assert gs.soft_contract(4.5, 9.0) == 4.5


def test_cone_floor_iff_deterministic():
    for i in range(1001):
        h = i / 1000
        s = gs.heat(h)
        assert gs.psi(s) >= gs.CONE - 1e-12
        if s == 0.0:
            assert h in (0.0, 1.0)


def test_residual_partition_sums_to_eight_and_atlas_covers_all_blocks():
    assert sum(gs.RESIDUAL_PARTITION) == 8
    assert {gs.atlas_role(b) for b in gs.RESIDUAL_PARTITION} == {"Watch", "Observer", "Forward"}
    assert gs.FIXED == 4.5


def test_phi_inverse_squared_is_self_inverse_under_cascade():
    assert gs.cascade_lambda(gs.residual_count(0.0)) * PHI ** 8 == pytest.approx(1.0, abs=1e-12)


def test_golden_ratio_uniqueness_of_cone():
    assert gs.CONE + gs.CONE == pytest.approx(gs.PHI_INV, abs=1e-15)


def test_bernoulli_variance_identity_holds_for_all_grid_points():
    for i in range(0, 1001):
        p = i / 1000
        assert gs.heat(p) == pytest.approx(p - p * p, abs=1e-15)


def test_runs_to_detect_monotonic_in_p():
    prev = None
    for p in [0.001, 0.01, 0.05, 0.1, 0.2, 0.5]:
        n = gs.runs_to_detect(p, 0.05)
        if prev is not None:
            assert n <= prev
        prev = n


def test_orientation_blindness_is_algebraic_not_numerical():
    for p in [0.0, 0.17, 0.33, 0.5, 0.66, 0.83, 1.0]:
        assert gs.heat(p) == gs.heat(1 - p)


def test_parity_with_javascript_fixture_when_node_available():
    if NODE is None:
        pytest.skip("node not installed")
    assert FIXTURE_CJS.exists(), "missing fixtures/great_scale_residual.cjs"
    hs = [0, 0.06, 0.25, 0.5, 0.75, 1, -1, 2]
    taus = [0, 0.3, 1]
    xs = [0, 4.5, 8.9, 13, -3]
    js = ("const r=require(%s);" % json.dumps(str(FIXTURE_CJS))
          + "const hs=%s;const taus=%s;const xs=%s;" % (hs, taus, xs)
          + "console.log(JSON.stringify({cone:r.CONE,"
          + "heat:hs.map(r.heat),psi:hs.map(h=>r.psi(r.heat(h))),"
          + "lam:[0,1,2.5,8].map(r.cascadeLambda),"
          + "topo:taus.map(t=>r.fromTopologicalSaturation(t)),"
          + "soft:xs.map(x=>r.softContract(x)),"
          + "rc:[0,0.25].map(r.residualCount)}))")
    ref = json.loads(subprocess.run([NODE, "-e", js], capture_output=True,
                                    text=True, check=True).stdout)
    assert gs.CONE == pytest.approx(ref["cone"], abs=1e-15)
    assert [gs.heat(h) for h in hs] == pytest.approx(ref["heat"], abs=1e-15)
    assert [gs.psi(gs.heat(h)) for h in hs] == pytest.approx(ref["psi"], abs=1e-15)
    assert [gs.cascade_lambda(n) for n in (0, 1, 2.5, 8)] == pytest.approx(ref["lam"], rel=1e-12)
    for t, r in zip(taus, ref["topo"]):
        py = gs.from_topological_saturation(t)
        for k in ("S", "n", "lambda", "logCoord", "psi"):
            assert py[k] == pytest.approx(r[k], rel=1e-12, abs=1e-15)
    assert [gs.soft_contract(x) for x in xs] == pytest.approx(ref["soft"], abs=1e-12)
    assert [gs.residual_count(s) for s in (0, 0.25)] == ref["rc"]
