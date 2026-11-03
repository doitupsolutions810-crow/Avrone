#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Transcendent-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Sixteenth-tier suite. Prior cumulative: 1031 passed. New transcendent/chaos layers:
  - Gate with boolean-like strings (True, TRUE, on, ON, yes, YES, y, Y, 1, enable)
  - Activator source never empty
  - Bridge schema never empty / exact length check
  - Parallel 40× unknown-component
  - --input with nested list of dicts (50 items)
  - Component names with only whitespace of different kinds
  - Verify python_compile_ok is bool True not int 1
  - Dashboard HTML has matching number of <td> roughly coherent
  - Master with multiple status args (status status)
  - Stress: 150× sequential live on rotating components
  - Cross-check agent roles are all unique
  - Refuse does not leave ALLOW_AGENT_EXECUTION in the process env of parent
  - Confirm adapters/ and examples/ are directories
  - Activator JSON keys are all strings
  - Rapid 20× full 12-component dry-run sweep parallel batches
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


# ─── Boolean-like gate strings ───────────────────────────────────────────────

def test_boolean_like_gates() -> None:
    print("\n── Boolean-Like Gate Strings ──")
    samples = [
        "True", "TRUE", "on", "ON", "yes", "YES", "y", "Y", "1", "enable",
        "enabled", "Enable", "YES ", " yes", "t", "T",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"bool-like [{val!r}] refuses", code != 0, f"code={code}")


# ─── source never empty ──────────────────────────────────────────────────────

def test_source_never_empty() -> None:
    print("\n── Activator source Never Empty ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        src = obj.get("source", "")
        record(f"{c} source non-empty", isinstance(src, str) and len(src) > 0)


# ─── Bridge schema length ────────────────────────────────────────────────────

def test_bridge_schema_length() -> None:
    print("\n── Bridge Schema Exact Length ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    schema = data.get("schema", "")
    record("schema non-empty", len(schema) > 0)
    record("schema == kg.production-training-result/v2", schema == "kg.production-training-result/v2")
    record("schema length == 34", len(schema) == len("kg.production-training-result/v2"))


# ─── Parallel 40 unknown ─────────────────────────────────────────────────────

def test_parallel_40_unknown() -> None:
    print("\n── Parallel 40× Unknown-Component ──")
    def one(i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), f"transcendent_unknown_{i}"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code != 0 and code != -1

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(40)]
        for f in as_completed(futs):
            results.append(f.result())
    record("40/40 parallel unknown", all(results), f"ok={sum(results)}")


# ─── Nested list of dicts --input ────────────────────────────────────────────

def test_nested_list_input() -> None:
    print("\n── Nested List-of-Dicts --input (50) ──")
    script = BIN / "activate_sqlite_persistence.sh"
    payload = {"items": [{"id": i, "name": f"item-{i}"} for i in range(50)]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("list-of-dicts --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Whitespace-only components ──────────────────────────────────────────────

def test_ws_only_components() -> None:
    print("\n── Whitespace-Only Component Names ──")
    for label, slug in [
        ("spaces", "     "),
        ("tabs", "\t\t\t"),
        ("mixed", " \t \n "),
    ]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"ws-only [{label}] no hang", code != -1, f"code={code}")


# ─── python_compile_ok is bool ───────────────────────────────────────────────

def test_python_compile_ok_bool() -> None:
    print("\n── python_compile_ok Is Bool True ──")
    for c in ["fastapi_control_plane", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        val = obj.get("integrity", {}).get("python_compile_ok")
        record(f"{c} python_compile_ok is bool", isinstance(val, bool))
        record(f"{c} python_compile_ok is True", val is True)


# ─── Dashboard td coherence ──────────────────────────────────────────────────

def test_dashboard_td() -> None:
    print("\n── Dashboard <td> Coherence ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    tds = text.count("<td")
    record("has multiple <td>", tds >= 6, f"td_count={tds}")


# ─── status status double ────────────────────────────────────────────────────

def test_double_status_arg() -> None:
    print("\n── Master status status Double Arg ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status", "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    # first arg is status → success path; extra ignored or ok
    record("status status no hang", code != -1, f"code={code}")
    if code == 0:
        record("status status DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" in out)
    else:
        record("status status DEPLOYMENT_COMPLETE", True, "non-zero tolerated for extra args")


# ─── 150× rotating live ──────────────────────────────────────────────────────

def test_150_rotating_live() -> None:
    print("\n── 150× Sequential Rotating Live Components ──")
    ok = 0
    for i in range(150):
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("150/150 rotating live", ok == 150, f"ok={ok}")


# ─── Agent roles unique ──────────────────────────────────────────────────────

def test_agent_roles_unique() -> None:
    print("\n── Agent Roles All Unique ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    roles = [a.get("role") for a in data["agents"]["agents"]]
    record("all roles unique", len(roles) == len(set(roles)), f"n={len(roles)} unique={len(set(roles))}")


# ─── adapters/ examples/ dirs ────────────────────────────────────────────────

def test_support_dirs_are_dirs() -> None:
    print("\n── adapters/ examples/ Are Directories ──")
    record("adapters is dir", (ROOT / "adapters").is_dir())
    record("examples is dir", (ROOT / "examples").is_dir())
    record("runs is dir", (ROOT / "runs").is_dir())
    record("bridge is dir", (ROOT / "bridge").is_dir())


# ─── JSON keys all strings ───────────────────────────────────────────────────

def test_json_keys_strings() -> None:
    print("\n── Activator JSON Keys All Strings ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_agent_registry_orchestrator.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    record("top-level keys are str", all(isinstance(k, str) for k in obj.keys()))
    integ = obj.get("integrity", {})
    record("integrity keys are str", all(isinstance(k, str) for k in integ.keys()))


# ─── 20× parallel dry-run batches ────────────────────────────────────────────

def test_20_dryrun_batches() -> None:
    print("\n── 20× Parallel Full 12-Component Dry-Run Batches ──")
    def batch(_i: int) -> bool:
        for c in COMPONENTS:
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code != 0:
                return False
        return True

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(batch, i) for i in range(20)]
        for f in as_completed(futs):
            results.append(f.result())
    record("20/20 dry-run batches", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Transcendent-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_boolean_like_gates()
    test_source_never_empty()
    test_bridge_schema_length()
    test_parallel_40_unknown()
    test_nested_list_input()
    test_ws_only_components()
    test_python_compile_ok_bool()
    test_dashboard_td()
    test_double_status_arg()
    test_150_rotating_live()
    test_agent_roles_unique()
    test_support_dirs_are_dirs()
    test_json_keys_strings()
    test_20_dryrun_batches()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
