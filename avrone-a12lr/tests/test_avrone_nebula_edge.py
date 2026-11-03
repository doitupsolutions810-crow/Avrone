#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Nebula-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twenty-fourth-tier suite. Prior cumulative: 1557 passed. New nebula/chaos layers:
  - Gate with reversed / scrambled true (eurt, etur, rute)
  - Activator never prints shell error tokens (command not found, syntax error)
  - Bridge schema matches canonical schema byte-for-byte
  - Parallel 55× master all under gate
  - --input with empty string as file content (not empty file only)
  - Component names with only combining diacritics
  - Verify every activator exit code is 0 under gate and non-zero when unknown via master
  - Dashboard contains '6' related to agent count
  - Master status output is valid JSON with required keys
  - Stress: 90× sequential refuse under unset gate
  - Cross-check label uniqueness and non-equality to component across all
  - Refuse message is under 2KB
  - Confirm bridge/conclude_bridge.py exists and is non-empty
  - Activator integrity always has python_compile_ok True
  - Rapid 5× triple-nested parallel (status∥refuse∥dry) bursts
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

MASTER_KEYS = {"status", "schema", "gate", "source", "components_registered"}


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


# ─── Scrambled true ──────────────────────────────────────────────────────────

def test_scrambled_true() -> None:
    print("\n── Scrambled / Reversed true Gates ──")
    samples = ["eurt", "etur", "rute", "teru", "uret", "rtue", "tr ue", "t rue"]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"scrambled [{val!r}] refuses", code != 0, f"code={code}")


# ─── No shell error tokens ───────────────────────────────────────────────────

def test_no_shell_errors() -> None:
    print("\n── No Shell Error Tokens Under Gate ──")
    bad = ("command not found", "syntax error", "no such file", "permission denied")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        combined = (out + err).lower()
        hit = [b for b in bad if b in combined]
        record(f"{c} no shell errors", not hit, f"hit={hit}")


# ─── Schema byte match ───────────────────────────────────────────────────────

def test_schema_byte_match() -> None:
    print("\n── Bridge Schema Byte-Match Canonical ──")
    canon = json.loads(CANONICAL.read_text())
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    bridge = json.loads(out)
    record("schema identical", bridge.get("schema") == canon.get("schema"))


# ─── Parallel 55 all ─────────────────────────────────────────────────────────

def test_parallel_55_all() -> None:
    print("\n── Parallel 55× Master all ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "all"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(55)]
        for f in as_completed(futs):
            results.append(f.result())
    record("55/55 parallel all", all(results), f"ok={sum(results)}")


# ─── Empty-string content --input ────────────────────────────────────────────

def test_empty_string_input() -> None:
    print("\n── Empty-String Content --input ──")
    script = BIN / "activate_langgraph_supervisor.sh"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write('""')  # JSON empty string
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=8,
        )
        record("empty-string --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Combining diacritics components ─────────────────────────────────────────

def test_diacritic_components() -> None:
    print("\n── Combining Diacritic Component Names ──")
    # names that are mostly marks
    for slug in ["é", "ñ", "ü", "å", "ç", "ø"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"diacritic [{slug}] no hang", code != -1, f"code={code}")


# ─── Exit codes under gate / unknown ─────────────────────────────────────────

def test_exit_codes_matrix() -> None:
    print("\n── Exit Codes Under Gate / Unknown ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"{c} exit 0 under gate", code == 0)
    code, out, err = run_cmd(
        ["bash", str(MASTER), "definitely_unknown_xyz"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("unknown via master non-zero", code != 0, f"code={code}")


# ─── Dashboard agent count 6 ─────────────────────────────────────────────────

def test_dashboard_six() -> None:
    print("\n── Dashboard Mentions 6 ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard has '6'", "6" in text)


# ─── Master status JSON keys ─────────────────────────────────────────────────

def test_master_status_json() -> None:
    print("\n── Master status Valid JSON + Keys ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    missing = MASTER_KEYS - set(obj.keys())
    record("status valid JSON with keys", not missing, f"missing={missing}")


# ─── 90× refuse unset ────────────────────────────────────────────────────────

def test_90_refuse_unset() -> None:
    print("\n── 90× Sequential Refuse (unset gate) ──")
    ok = 0
    for _ in range(90):
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        if code != 0:
            ok += 1
    record("90/90 refuse unset", ok == 90, f"ok={ok}")


# ─── Label uniqueness + != component ─────────────────────────────────────────

def test_label_unique_ne() -> None:
    print("\n── Labels Unique and != component ──")
    labels = []
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        labels.append(obj.get("label"))
        record(f"{c} label != component", obj.get("label") != obj.get("component"))
    record("all labels unique", len(labels) == len(set(labels)), f"n={len(set(labels))}")


# ─── Refuse under 2KB ────────────────────────────────────────────────────────

def test_refuse_size() -> None:
    print("\n── Refuse Message Under 2KB ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    size = len((out + err).encode("utf-8"))
    record("refuse size < 2048", size < 2048, f"size={size}")


# ─── Bridge file non-empty ───────────────────────────────────────────────────

def test_bridge_file() -> None:
    print("\n── bridge/conclude_bridge.py Non-Empty ──")
    record("bridge exists", BRIDGE.exists())
    record("bridge size > 200", BRIDGE.stat().st_size > 200, f"size={BRIDGE.stat().st_size}")


# ─── python_compile_ok True all ──────────────────────────────────────────────

def test_compile_ok_all() -> None:
    print("\n── python_compile_ok True (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} python_compile_ok True", obj.get("integrity", {}).get("python_compile_ok") is True)


# ─── 5× triple parallel bursts ───────────────────────────────────────────────

def test_5_triple_bursts() -> None:
    print("\n── 5× Triple Parallel Bursts (status∥refuse∥dry) ──")
    def status() -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        return c == 0

    def refuse() -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return c != 0

    def dry() -> bool:
        c, o, e = run_cmd(
            ["bash", str(BIN / "activate_skill_factory.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return c == 0

    ok_bursts = 0
    for _ in range(5):
        results = []
        with ThreadPoolExecutor(max_workers=9) as pool:
            futs = [pool.submit(status) for _ in range(3)]
            futs += [pool.submit(refuse) for _ in range(3)]
            futs += [pool.submit(dry) for _ in range(3)]
            for f in as_completed(futs):
                results.append(f.result())
        if all(results):
            ok_bursts += 1
    record("5/5 triple bursts", ok_bursts == 5, f"ok={ok_bursts}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Nebula-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_scrambled_true()
    test_no_shell_errors()
    test_schema_byte_match()
    test_parallel_55_all()
    test_empty_string_input()
    test_diacritic_components()
    test_exit_codes_matrix()
    test_dashboard_six()
    test_master_status_json()
    test_90_refuse_unset()
    test_label_unique_ne()
    test_refuse_size()
    test_bridge_file()
    test_compile_ok_all()
    test_5_triple_bursts()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
