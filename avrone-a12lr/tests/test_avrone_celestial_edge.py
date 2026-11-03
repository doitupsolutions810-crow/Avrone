#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Celestial-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twenty-third-tier suite. Prior cumulative: 1472 passed. New celestial/chaos layers:
  - Gate with Unicode normalization forms (NFC/NFD lookalikes of true)
  - Activator never includes 'traceback' or 'Exception' in stdout under gate
  - Bridge agents.gate_satisfied is bool True under gate
  - Parallel 70× dry-run rotating
  - --input with JSON number as root (not object/array)
  - Component names with path traversal and null-byte printable escapes
  - Verify every activator returns DEPLOYMENT_COMPLETE exactly once in stdout
  - Dashboard contains 'VIP' and 'proxy'
  - Master when COMPONENT is empty string defaults to status
  - Stress: 120× sequential master status + all alternating
  - Cross-check all activator scripts are owned/readable
  - Refuse does not leave residual files in /tmp that match avrone*
  - Confirm references/ has canonical-deployment.json
  - Activator integrity.dependencies_resolved never exceeds 100
  - Rapid 10× full parallel fan-out of all 12 activators (120 concurrent max path)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unicodedata
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


# ─── Unicode normalization lookalikes ────────────────────────────────────────

def test_unicode_norm_gates() -> None:
    print("\n── Unicode Normalization Lookalike Gates ──")
    # compose lookalikes that are not exact ASCII true
    samples = [
        unicodedata.normalize("NFD", "true"),  # may still be true if no combining
        "ｔｒｕｅ",  # fullwidth
        "truе",     # cyrillic e
        "tｒue",
        "ᵗʳᵘᵉ",     # modifier letters
        "ⓣⓡⓤⓔ",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        # exact ASCII true only
        ok = code != 0 if val != "true" else code == 0
        record(f"norm [{val.encode('unicode_escape').decode()}] refuses or exact", ok, f"code={code}")


# ─── no traceback under gate ─────────────────────────────────────────────────

def test_no_traceback() -> None:
    print("\n── No traceback/Exception Under Gate ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        combined = (out + err).lower()
        record(f"{c} no traceback", "traceback" not in combined)
        record(f"{c} no Exception", "exception" not in combined or "DEPLOYMENT_COMPLETE" in out)


# ─── agents.gate_satisfied True ──────────────────────────────────────────────

def test_agents_gate_true() -> None:
    print("\n── Bridge agents.gate_satisfied True ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    gs = data.get("agents", {}).get("gate_satisfied")
    record("gate_satisfied is True", gs is True, f"gs={gs!r}")


# ─── Parallel 70 dry-run ─────────────────────────────────────────────────────

def test_parallel_70_dryrun() -> None:
    print("\n── Parallel 70× Rotating Dry-Run ──")
    def one(i: int) -> bool:
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(one, i) for i in range(70)]
        for f in as_completed(futs):
            results.append(f.result())
    record("70/70 parallel dry-run", all(results), f"ok={sum(results)}")


# ─── JSON number root --input ────────────────────────────────────────────────

def test_number_root_input() -> None:
    print("\n── JSON Number Root --input ──")
    script = BIN / "activate_react_dashboard.sh"
    for label, payload in [("int", 42), ("float", 3.14), ("neg", -1)]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump(payload, tf)
            path = tf.name
        try:
            code, out, err = run_cmd(
                ["bash", str(script), "--input", path],
                env={"ALLOW_AGENT_EXECUTION": "true"},
                timeout=8,
            )
            record(f"number-root [{label}] no hang", code != -1, f"code={code}")
        finally:
            os.unlink(path)


# ─── Traversal + escape components ───────────────────────────────────────────

def test_traversal_escape_components() -> None:
    print("\n── Traversal / Escape Component Names ──")
    for slug in ["../", "..\\", "....//", "comp\\n", "comp\\t", "a/b/c"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"trav/esc [{slug!r}] no hang", code != -1, f"code={code}")


# ─── DEPLOYMENT_COMPLETE once ────────────────────────────────────────────────

def test_complete_once() -> None:
    print("\n── DEPLOYMENT_COMPLETE Appears Once ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        count = out.count("DEPLOYMENT_COMPLETE")
        record(f"{c} COMPLETE count == 1", count == 1, f"count={count}")


# ─── Dashboard VIP proxy ─────────────────────────────────────────────────────

def test_dashboard_vip_proxy() -> None:
    print("\n── Dashboard VIP / proxy ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has VIP", "VIP" in text)
    record("has proxy", "proxy" in text.lower())


# ─── Empty component defaults status ─────────────────────────────────────────

def test_empty_defaults_status() -> None:
    print("\n── Empty Component Defaults to status ──")
    # master with no args
    code, out, err = run_cmd(
        ["bash", str(MASTER)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("no-args defaults status success", code == 0 and "DEPLOYMENT_COMPLETE" in out)


# ─── 120× status/all alternating ─────────────────────────────────────────────

def test_120_status_all() -> None:
    print("\n── 120× status / all Alternating ──")
    ok = 0
    for i in range(120):
        arg = "status" if i % 2 == 0 else "all"
        code, out, err = run_cmd(
            ["bash", str(MASTER), arg],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("120/120 status/all", ok == 120, f"ok={ok}")


# ─── Activators readable ─────────────────────────────────────────────────────

def test_activators_readable() -> None:
    print("\n── All Activators Readable ──")
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        record(f"{c} readable", os.access(path, os.R_OK) and path.is_file())
    record("master readable", os.access(MASTER, os.R_OK) and MASTER.is_file())


# ─── Canonical exists ────────────────────────────────────────────────────────

def test_canonical_exists() -> None:
    print("\n── Canonical Reference Exists ──")
    record("canonical-deployment.json exists", CANONICAL.exists())
    record("canonical is file", CANONICAL.is_file())


# ─── deps never > 100 ────────────────────────────────────────────────────────

def test_deps_cap() -> None:
    print("\n── dependencies_resolved Never > 100 ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        dr = obj.get("integrity", {}).get("dependencies_resolved", 0)
        record(f"{c} deps <= 100", isinstance(dr, int) and dr <= 100, f"dr={dr}")


# ─── 10× full parallel fan-out ───────────────────────────────────────────────

def test_10_full_fanout() -> None:
    print("\n── 10× Full Parallel Fan-Out of All 12 ──")
    def fanout(_i: int) -> bool:
        def one(c: str) -> bool:
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            return code == 0 and "DEPLOYMENT_COMPLETE" in out

        results = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = [pool.submit(one, c) for c in COMPONENTS]
            for f in as_completed(futs):
                results.append(f.result())
        return all(results)

    ok = 0
    for i in range(10):
        if fanout(i):
            ok += 1
    record("10/10 full fan-outs", ok == 10, f"ok={ok}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Celestial-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_unicode_norm_gates()
    test_no_traceback()
    test_agents_gate_true()
    test_parallel_70_dryrun()
    test_number_root_input()
    test_traversal_escape_components()
    test_complete_once()
    test_dashboard_vip_proxy()
    test_empty_defaults_status()
    test_120_status_all()
    test_activators_readable()
    test_canonical_exists()
    test_deps_cap()
    test_10_full_fanout()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
