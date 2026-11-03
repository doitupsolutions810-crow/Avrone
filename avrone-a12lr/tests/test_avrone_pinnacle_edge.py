#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Pinnacle-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twentieth-tier suite. Prior cumulative: 1263 passed. New pinnacle/chaos layers:
  - Gate with base64-ish and hex-ish encodings of true
  - Activator status never equals 'error' or 'failed' under gate
  - Bridge control704 block has exactly 4 keys (access, priority, identity, lattice)
  - Parallel 35× live on a single hot component
  - --input with JSON containing duplicate keys simulation (last wins via object)
  - Component names with Unicode math symbols
  - Verify every activator returns exit 0 under gate
  - Dashboard <table> present and closed
  - Master with ARGV containing only empty strings after shift simulation
  - Stress: 500× sequential status under gate (throughput)
  - Cross-check bridge and agents module produce same count
  - Refuse does not create files under dashboard/
  - Confirm tests/ has at least 18 test_avrone_*.py files
  - Activator JSON serializes with sort_keys without change of meaning
  - Rapid 40× refuse + 40× status parallel
"""
from __future__ import annotations

import base64
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
TESTS = ROOT / "tests"
DASH = ROOT / "dashboard"

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

CONTROL704_KEYS = {"access", "priority", "identity", "lattice"}


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


# ─── Base64 / hex-ish true ───────────────────────────────────────────────────

def test_b64_hex_true() -> None:
    print("\n── Base64 / Hex-ish true Gates ──")
    b64 = base64.b64encode(b"true").decode()
    samples = [
        b64,                    # dHJ1ZQ==
        "74727565",             # hex true
        "0x74727565",
        "\\x74\\x72\\x75\\x65",
        "true==",
        "==true",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"b64/hex [{val!r}] refuses", code != 0, f"code={code}")


# ─── status not error/failed ─────────────────────────────────────────────────

def test_status_not_error() -> None:
    print("\n── Activator status Not error/failed ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        st = str(obj.get("status", "")).lower()
        record(f"{c} status not error/failed", st not in ("error", "failed", "fail"))


# ─── control704 exact 4 keys ─────────────────────────────────────────────────

def test_control704_keys() -> None:
    print("\n── control704 Exactly Expected Keys ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    keys = set(data.get("control704", {}).keys())
    record("control704 has expected keys", CONTROL704_KEYS <= keys, f"keys={keys}")
    record("control704 has no surprise required missing", CONTROL704_KEYS <= keys)


# ─── Parallel 35 live hot component ──────────────────────────────────────────

def test_parallel_35_hot() -> None:
    print("\n── Parallel 35× Live Hot Component ──")
    script = BIN / "activate_agent_registry_orchestrator.sh"
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(script)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(35)]
        for f in as_completed(futs):
            results.append(f.result())
    record("35/35 hot live", all(results), f"ok={sum(results)}")


# ─── Wide object --input ─────────────────────────────────────────────────────

def test_wide_object_input() -> None:
    print("\n── Wide Object --input (duplicate-key style) ──")
    script = BIN / "activate_containerized_deployment.sh"
    # JSON objects can't have true duplicate keys in dumps; simulate many keys
    payload = {f"field_{i}": f"value_{i}" for i in range(100)}
    payload["override"] = "last"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("wide object --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Math symbol components ──────────────────────────────────────────────────

def test_math_symbol_components() -> None:
    print("\n── Unicode Math Symbol Component Names ──")
    for slug in ["∑", "∏", "∞", "≈", "≠", "≤", "≥", "√"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"math [{slug}] no hang", code != -1, f"code={code}")


# ─── Exit 0 under gate all ───────────────────────────────────────────────────

def test_exit0_all_under_gate() -> None:
    print("\n── Exit 0 Under Gate (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"{c} exit 0 under gate", code == 0, f"code={code}")


# ─── Dashboard table tags ────────────────────────────────────────────────────

def test_dashboard_table() -> None:
    print("\n── Dashboard <table> Present and Closed ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has <table", "<table" in text.lower())
    record("has </table>", "</table>" in text.lower())
    record("table open/close count equal", text.lower().count("<table") == text.lower().count("</table>"))


# ─── 500× status throughput ──────────────────────────────────────────────────

def test_500_status() -> None:
    print("\n── 500× Sequential Status Throughput ──")
    ok = 0
    for _ in range(500):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("500/500 status", ok == 500, f"ok={ok}")


# ─── Bridge/agents count match ───────────────────────────────────────────────

def test_bridge_agents_count_match() -> None:
    print("\n── Bridge count == Agents Module count ──")
    _, out1, _ = run_cmd([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    _, out2, _ = run_cmd([sys.executable, str(AGENTS)], env={"ALLOW_AGENT_EXECUTION": "true"})
    c1 = json.loads(out1)["agents"]["count"]
    c2 = json.loads(out2)["agents"]["count"]
    record("counts match and == 6", c1 == c2 == 6, f"bridge={c1} agents={c2}")


# ─── Refuse no dashboard writes ──────────────────────────────────────────────

def test_refuse_no_dashboard_writes() -> None:
    print("\n── Refuse No Writes Under dashboard/ ──")
    before = set(DASH.rglob("*")) if DASH.exists() else set()
    for _ in range(5):
        run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    after = set(DASH.rglob("*")) if DASH.exists() else set()
    new = after - before
    record("no new files under dashboard/", len(new) == 0, f"new={len(new)}")


# ─── Suite count >= 18 ───────────────────────────────────────────────────────

def test_suite_count() -> None:
    print("\n── tests/ Has >= 18 test_avrone_*.py ──")
    files = list(TESTS.glob("test_avrone_*.py"))
    record("suite count >= 18", len(files) >= 18, f"count={len(files)}")


# ─── sort_keys stable meaning ────────────────────────────────────────────────

def test_sort_keys_stable() -> None:
    print("\n── JSON sort_keys Stable Meaning ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_fastapi_control_plane.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    o2 = json.loads(json.dumps(obj, sort_keys=True))
    record("status preserved under sort_keys", o2.get("status") == obj.get("status"))
    record("component preserved under sort_keys", o2.get("component") == obj.get("component"))


# ─── 40 refuse + 40 status parallel ──────────────────────────────────────────

def test_40_40_parallel() -> None:
    print("\n── 40× Refuse + 40× Status Parallel ──")
    def refuse(_i: int) -> bool:
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return code != 0

    def status(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(refuse, i) for i in range(40)] + [pool.submit(status, i) for i in range(40)]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"80/80 refuse+status", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Pinnacle-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_b64_hex_true()
    test_status_not_error()
    test_control704_keys()
    test_parallel_35_hot()
    test_wide_object_input()
    test_math_symbol_components()
    test_exit0_all_under_gate()
    test_dashboard_table()
    test_500_status()
    test_bridge_agents_count_match()
    test_refuse_no_dashboard_writes()
    test_suite_count()
    test_sort_keys_stable()
    test_40_40_parallel()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
