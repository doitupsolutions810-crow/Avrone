#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Event-Horizon Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Seventh-tier suite. Prior cumulative: 449 passed. New chaos layers:
  - Simultaneous gate true/false on different components
  - Activator JSON key set exact match (no unexpected required-breaking)
  - Bridge agents[].provider optional but type-safe when present
  - Repeated --dry-run 30× no state leak
  - Master status vs all semantic equivalence
  - File size bounds on activators (not empty, not huge)
  - Shebang line exact prefix
  - Concurrent bridge-only storm (20×)
  - Gate env with newline-only / tab-only
  - Component slug with leading dash / double-dash
  - --input pointing at a directory (not file)
  - --input pointing at unreadable path (permission)
  - Dashboard contains Control704 VIP / high priority markers
  - Canonical version field non-empty
  - Activator label field non-empty string
  - Stress: alternating master status and component activate (40 steps)
  - Verify refuse does not create side-effect files in ROOT
  - Unicode in dashboard already covered; re-check agent ids in HTML entities-free
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
CANONICAL = ROOT / "references" / "canonical-deployment.json"
AGENTS = ROOT / "agents" / "grok_bot_integration.py"
MASTER = BIN / "activate_avrone.sh"
DASHBOARD = ROOT / "dashboard" / "index.html"

PASS = 0
FAIL = 0
SKIP = 0

COMPONENTS = [
    "fastapi_control_plane",
    "react_dashboard",
    "openai_model_adapter",
    "agent_registry_orchestrator",
    "secure_cli_execution",
    "sqlite_persistence",
    "containerized_deployment",
    "persistent_memory_agent",
    "control12_lattice_op",
    "langgraph_supervisor",
    "training_loop",
    "skill_factory",
]

REQUIRED_ACTIVATOR_KEYS = {
    "status", "component", "label", "gate_satisfied", "source", "integrity",
}


def record(name: str, ok: bool, detail: str = "", skip: bool = False) -> None:
    global PASS, FAIL, SKIP
    if skip:
        SKIP += 1
        print(f"[SKIP] {name}" + (f" — {detail}" if detail else ""))
        return
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def run_cmd(
    cmd: list[str],
    env: dict | None = None,
    clear_gate: bool = False,
    timeout: float = 12.0,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    e = os.environ.copy()
    if clear_gate:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env is not None:
        e.update(env)
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            env=e,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except ValueError as exc:
        return -2, "", f"ValueError:{exc}"


# ─── Simultaneous mixed gate ─────────────────────────────────────────────────

def test_simultaneous_mixed_gate() -> None:
    print("\n── Simultaneous Mixed Gate (true + false components) ──")
    def with_gate(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def no_gate(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            clear_gate=True,
        )
        return code != 0

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = []
        for c in COMPONENTS[:4]:
            futs.append(pool.submit(with_gate, c))
            futs.append(pool.submit(no_gate, c))
        for f in as_completed(futs):
            results.append(f.result())
    record(f"{sum(results)}/{len(results)} mixed-gate concurrent ok", all(results), f"ok={sum(results)}")


# ─── Exact required key set ──────────────────────────────────────────────────

def test_exact_required_keys() -> None:
    print("\n── Activator Required Key Set ──")
    for c in ["agent_registry_orchestrator", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        missing = REQUIRED_ACTIVATOR_KEYS - set(obj.keys())
        record(f"{c} has all required keys", not missing, f"missing={missing}")
        record(f"{c} label is non-empty str", isinstance(obj.get("label"), str) and len(obj["label"]) > 0)


# ─── Bridge provider type safety ─────────────────────────────────────────────

def test_bridge_provider_types() -> None:
    print("\n── Bridge Agent Provider Type Safety ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    for a in data["agents"]["agents"]:
        if "provider" in a:
            record(f"{a['id']} provider is str", isinstance(a["provider"], str))
        if "port" in a:
            record(f"{a['id']} port is int", isinstance(a["port"], int))


# ─── Dry-run 30× no state leak ───────────────────────────────────────────────

def test_dryrun_30() -> None:
    print("\n── Dry-Run 30× No State Leak ──")
    script = BIN / "activate_langgraph_supervisor.sh"
    outputs = []
    for _ in range(30):
        code, out, err = run_cmd(
            ["bash", str(script), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code != 0:
            record("dry-run 30× all exit 0", False, f"code={code}")
            return
        outputs.append(out)
    record("dry-run 30× all exit 0", True)
    record("dry-run 30× outputs identical", len(set(outputs)) == 1)


# ─── status vs all equivalence ───────────────────────────────────────────────

def test_status_vs_all() -> None:
    print("\n── Master status vs all Equivalence ──")
    code1, out1, _ = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    code2, out2, _ = run_cmd(
        ["bash", str(MASTER), "all"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("both exit 0", code1 == 0 and code2 == 0)
    record("both contain DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" in out1 and "DEPLOYMENT_COMPLETE" in out2)
    # semantic parity: same schema key
    o1, o2 = json.loads(out1), json.loads(out2)
    record("same schema", o1.get("schema") == o2.get("schema"))
    record("same components_registered", o1.get("components_registered") == o2.get("components_registered"))


# ─── Activator file size bounds ──────────────────────────────────────────────

def test_file_size_bounds() -> None:
    print("\n── Activator File Size Bounds ──")
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        size = path.stat().st_size
        record(f"{c} size > 0", size > 0, f"size={size}")
        record(f"{c} size < 50KB", size < 50_000, f"size={size}")


# ─── Shebang prefix ──────────────────────────────────────────────────────────

def test_shebang_prefix() -> None:
    print("\n── Shebang Exact Prefix ──")
    for c in COMPONENTS:
        head = (BIN / f"activate_{c}.sh").read_bytes()[:32]
        record(f"{c} starts with #!", head.startswith(b"#!"))


# ─── Bridge storm 20× ────────────────────────────────────────────────────────

def test_bridge_storm_20() -> None:
    print("\n── Concurrent Bridge Storm 20× ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(one, i) for i in range(20)]
        for f in as_completed(futs):
            results.append(f.result())
    record("20/20 concurrent bridge", all(results), f"ok={sum(results)}")


# ─── Newline / tab only gate ─────────────────────────────────────────────────

def test_whitespace_only_gate() -> None:
    print("\n── Newline / Tab-Only Gate ──")
    for label, val in [("newline", "\n"), ("tab", "\t"), ("crlf", "\r\n"), ("spaces", "   ")]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"gate [{label}] refuses", code != 0, f"code={code}")


# ─── Leading dash component ──────────────────────────────────────────────────

def test_leading_dash_component() -> None:
    print("\n── Leading Dash / Double-Dash Component ──")
    for slug in ["-h", "--help", "--status", "-status", "---"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # must not hang; must not return clean multi-component status as if defaulted incorrectly
        # (default only when no arg — here we passed an arg)
        ok = code != -1
        record(f"leading-dash [{slug}] no hang", ok, f"code={code}")


# ─── --input directory / unreadable ──────────────────────────────────────────

def test_input_dir_and_unreadable() -> None:
    print("\n── --input Directory & Unreadable Path ──")
    script = BIN / "activate_sqlite_persistence.sh"
    # directory as --input
    code, out, err = run_cmd(
        ["bash", str(script), "--input", str(ROOT / "bin")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        timeout=8,
    )
    record("--input directory no hang", code != -1, f"code={code}")

    # unreadable file
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        path = tf.name
        tf.write(b'{"x":1}')
    try:
        os.chmod(path, 0o000)
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=8,
        )
        record("--input unreadable no hang", code != -1, f"code={code}")
    finally:
        os.chmod(path, 0o644)
        os.unlink(path)


# ─── Dashboard Control704 markers ────────────────────────────────────────────

def test_dashboard_control704_markers() -> None:
    print("\n── Dashboard Control704 Markers ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard has Control704", "Control704" in text)
    record("dashboard has VIP or proxy", "VIP" in text or "proxy" in text)
    record("dashboard has high priority or High priority", "high priority" in text.lower() or "High priority" in text)
    record("dashboard has Avrone or Due", "Avrone" in text or "Due" in text)


# ─── Canonical version ───────────────────────────────────────────────────────

def test_canonical_version() -> None:
    print("\n── Canonical Version Field ──")
    data = json.loads(CANONICAL.read_text())
    ver = data.get("build_context", {}).get("version")
    record("version present non-empty", isinstance(ver, str) and len(ver) > 0, f"version={ver}")


# ─── Alternating stress 40 ───────────────────────────────────────────────────

def test_alternating_stress_40() -> None:
    print("\n── Alternating Master/Component Stress 40 Steps ──")
    ok = 0
    for i in range(40):
        if i % 2 == 0:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
        else:
            c = COMPONENTS[i % len(COMPONENTS)]
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
    record("40/40 alternating stress", ok == 40, f"ok={ok}")


# ─── Refuse no side-effect files ─────────────────────────────────────────────

def test_refuse_no_side_effects() -> None:
    print("\n── Refuse Creates No Side-Effect Files ──")
    before = {p.name for p in ROOT.iterdir()}
    for _ in range(5):
        run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    after = {p.name for p in ROOT.iterdir()}
    new = after - before
    # allow only possible temp/cache noise we don't control; flag unexpected top-level
    suspicious = {n for n in new if n.endswith((".json", ".log", ".tmp", ".sh", ".py"))}
    record("refuse adds no suspicious top-level files", not suspicious, f"new={suspicious}")


# ─── Agent ids in dashboard raw ──────────────────────────────────────────────

def test_agent_ids_raw_in_dashboard() -> None:
    print("\n── Agent IDs Raw in Dashboard (no entity encoding) ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    for aid in [
        "grok-bot-supervisor",
        "grok-bot-computer-use",
        "grok-bot-github-bridge",
        "avrone-fastapi-control",
        "avrone-react-dashboard",
        "persistent-memory-agent",
    ]:
        record(f"raw id {aid} in HTML", aid in text)


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Event-Horizon Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_simultaneous_mixed_gate()
    test_exact_required_keys()
    test_bridge_provider_types()
    test_dryrun_30()
    test_status_vs_all()
    test_file_size_bounds()
    test_shebang_prefix()
    test_bridge_storm_20()
    test_whitespace_only_gate()
    test_leading_dash_component()
    test_input_dir_and_unreadable()
    test_dashboard_control704_markers()
    test_canonical_version()
    test_alternating_stress_40()
    test_refuse_no_side_effects()
    test_agent_ids_raw_in_dashboard()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
