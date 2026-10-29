import importlib.util, json
from pathlib import Path

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

H = _load("harmonic_dash", str(Path(__file__).resolve().parents[1] / "harmonic" / "harmonic_dash.py"))

def test_gauge_spec_shape():
    s = H.GAUGE_SPEC
    assert s["id"] == "harmonic-thermo-gauge" and s["type"] == "radial-gauge"
    assert s["domain"] == [0.0, 1.0] and len(s["zones"]) == 3
    assert s["needle"] == "energy_ratio" and s["poll_ms"] == 2000

def test_zone_colors_match_tone_mapping():
    assert [z["color"] for z in H.GAUGE_SPEC["zones"]] == ["#3dd68c", "#f5a623", "#e24b4b"]

def test_d3_snippet_has_arc_and_needle():
    assert "d3.arc" in H.D3_SNIPPET and "setDebt" in H.D3_SNIPPET and "/api/codex/events" in H.D3_SNIPPET

def test_injector_idempotent():
    html = "<html><body></body></html>"
    out = H.inject_into_index(html)
    assert out.count('id="harmonic-thermo-gauge"') == 1
    assert H.inject_into_index(out).count('id="harmonic-thermo-gauge"') == 1

def test_worst_debt_aggregation_logic():
    lines = [
        '{"restart_cost": {"debt": 0.2}}',
        '{"restart_cost": {"debt": 0.8}}',
        '{"restart_cost": {"debt": 0.5}}',
        "not-json",
    ]
    worst = 0
    for line in lines:
        try: ev = json.loads(line)
        except Exception: continue
        rc = ev.get("restart_cost")
        if rc and isinstance(rc.get("debt"), (int, float)):
            worst = max(worst, rc["debt"])
    assert worst == 0.8
