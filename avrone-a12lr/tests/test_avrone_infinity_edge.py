#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Infinity-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twelfth-tier suite. Prior cumulative: 778 passed. New infinity/chaos layers:
  - Gate with zero-width characters interleaving "t/r/u/e"
  - Activator JSON must not contain NaN/Infinity literals
  - Bridge control704.access contains both VIP and proxy
  - Parallel 100× dry-run only
  - --input with JSON number extremes (2**53, -2**53, 0, 1e308)
  - Component name equal to a shell builtin (echo, test, [, true)
  - Verify dependencies_resolved == 100 on every activator
  - Dashboard footer contains Control704 IT Technician
  - Master with env -i minimal environment (if safe)
  - Stress: 1000× sequential status under gate (throughput ceiling)
  - Cross-check every agent id appears in bridge and agents module identically
  - gate_satisfied is bool not string "true"
  - Refuse path does not write to runs/
  - Confirm bin/activate_avrone.sh is readable
  - Rapid fire 25× unknown + 25× status mixed parallel
"""
from __future__ import annotations

import json
import math
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
RUNS = ROOT / "runs"

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


# ─── Zero-width interleave true ──────────────────────────────────────────────

def test_zw_interleave_true() -> None:
    print("\n── Zero-Width Interleave of t/r/u/e ──")
    zw = "\u200b"  # zero-width space
    samples = [
        f"t{zw}rue",
        f"tr{zw}ue",
        f"tru{zw}e",
        f"t{zw}r{zw}u{zw}e",
        f"{zw}true{zw}",
        "true\u200b",
        "\u200btrue",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"zw-interleave [{val.encode('unicode_escape').decode()}] refuses", code != 0, f"code={code}")


# ─── No NaN/Infinity in JSON ─────────────────────────────────────────────────

def test_no_nan_infinity() -> None:
    print("\n── Activator JSON No NaN/Infinity ──")
    for c in ["fastapi_control_plane", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # raw text must not contain these literals
        bad = "NaN" in out or "Infinity" in out or "-Infinity" in out
        record(f"{c} no NaN/Infinity literals", not bad)
        obj = json.loads(out)
        # walk numbers
        def walk(o):
            if isinstance(o, float):
                return math.isfinite(o)
            if isinstance(o, dict):
                return all(walk(v) for v in o.values())
            if isinstance(o, list):
                return all(walk(v) for v in o)
            return True
        record(f"{c} all floats finite", walk(obj))


# ─── control704.access VIP + proxy ───────────────────────────────────────────

def test_access_vip_proxy() -> None:
    print("\n── control704.access Contains VIP and proxy ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    access = str(data.get("control704", {}).get("access", ""))
    record("access contains VIP", "VIP" in access)
    record("access contains proxy", "proxy" in access.lower())


# ─── Parallel 100 dry-run ────────────────────────────────────────────────────

def test_parallel_100_dryrun() -> None:
    print("\n── Parallel 100× Dry-Run ──")
    def one(i: int) -> bool:
        c = COMPONENTS[i % len(COMPONENTS)]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(one, i) for i in range(100)]
        for f in as_completed(futs):
            results.append(f.result())
    record("100/100 parallel dry-run", all(results), f"ok={sum(results)}")


# ─── JSON number extremes --input ────────────────────────────────────────────

def test_number_extremes_input() -> None:
    print("\n── JSON Number Extremes --input ──")
    script = BIN / "activate_openai_model_adapter.sh"
    payload = {
        "max_safe": 2**53,
        "min_safe": -(2**53),
        "zero": 0,
        "one": 1,
        "big": 1e308,
        "small": 1e-308,
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("number extremes --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Shell builtin component names ───────────────────────────────────────────

def test_shell_builtin_names() -> None:
    print("\n── Shell Builtin Component Names ──")
    for slug in ["echo", "test", "[", "true", "false", ":", "pwd", "cd"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"builtin [{slug}] no hang / not success status", code != -1 and not (
            code == 0 and "components_registered" in out
        ), f"code={code}")


# ─── dependencies_resolved == 100 all ────────────────────────────────────────

def test_deps_100_all() -> None:
    print("\n── dependencies_resolved == 100 (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        dr = obj.get("integrity", {}).get("dependencies_resolved")
        record(f"{c} deps == 100", dr == 100, f"dr={dr}")


# ─── Dashboard footer identity ───────────────────────────────────────────────

def test_dashboard_footer() -> None:
    print("\n── Dashboard Footer Control704 IT Technician ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("footer has Control704 IT Technician", "Control704 IT Technician" in text)


# ─── 1000× sequential status ─────────────────────────────────────────────────

def test_1000_status() -> None:
    print("\n── 1000× Sequential Status Throughput ──")
    ok = 0
    for _ in range(1000):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("1000/1000 status", ok == 1000, f"ok={ok}")


# ─── Agent id identity across modules ────────────────────────────────────────

def test_agent_id_identity() -> None:
    print("\n── Agent IDs Identical Across Bridge & Agents Module ──")
    code1, out1, _ = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    code2, out2, _ = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    b = json.loads(out1)
    a = json.loads(out2)
    ids_b = sorted(x["id"] for x in b["agents"]["agents"])
    ids_a = sorted(x["id"] for x in a["agents"]["agents"])
    record("agent ids identical", ids_b == ids_a, f"bridge={ids_b} agents={ids_a}")


# ─── gate_satisfied is bool ──────────────────────────────────────────────────

def test_gate_satisfied_bool() -> None:
    print("\n── gate_satisfied Is Bool Not String ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_langgraph_supervisor.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    gs = obj.get("gate_satisfied")
    record("gate_satisfied type is bool", isinstance(gs, bool), f"type={type(gs).__name__} val={gs!r}")
    record("gate_satisfied is True", gs is True)


# ─── Refuse no runs/ writes ──────────────────────────────────────────────────

def test_refuse_no_runs_writes() -> None:
    print("\n── Refuse Does Not Write to runs/ ──")
    before = set(RUNS.iterdir()) if RUNS.exists() else set()
    for _ in range(10):
        run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    after = set(RUNS.iterdir()) if RUNS.exists() else set()
    new = after - before
    record("refuse adds no files in runs/", len(new) == 0, f"new={new}")


# ─── Master readable ─────────────────────────────────────────────────────────

def test_master_readable() -> None:
    print("\n── Master Activator Readable ──")
    record("activate_avrone.sh exists", MASTER.exists())
    record("activate_avrone.sh readable", os.access(MASTER, os.R_OK))


# ─── Mixed unknown + status parallel ─────────────────────────────────────────

def test_mixed_unknown_status_parallel() -> None:
    print("\n── 25× Unknown + 25× Status Parallel ──")
    def unknown(i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), f"nope_{i}"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code != 0

    def status(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(unknown, i) for i in range(25)] + [pool.submit(status, i) for i in range(25)]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"50/50 mixed unknown+status", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Infinity-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_zw_interleave_true()
    test_no_nan_infinity()
    test_access_vip_proxy()
    test_parallel_100_dryrun()
    test_number_extremes_input()
    test_shell_builtin_names()
    test_deps_100_all()
    test_dashboard_footer()
    test_1000_status()
    test_agent_id_identity()
    test_gate_satisfied_bool()
    test_refuse_no_runs_writes()
    test_master_readable()
    test_mixed_unknown_status_parallel()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
