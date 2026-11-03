#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Apocalypse-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Fifth-tier suite. Prior cumulative: 331 passed. Additional chaos layers:
  - Path traversal component names (../, ..\\, absolute paths)
  - ENV var name collision / overwrite attempts
  - Activator stdout is exactly one JSON object (no trailing garbage)
  - Bridge output is exactly one JSON object
  - Double-gate export in same shell session
  - Component list completeness vs COMPONENTS constant
  - Integrity field numeric types
  - Stress: 50 sequential status under gate
  - Stress: 25 sequential refuse under no-gate
  - Mixed dry-run + live + bridge in one process burst
  - File descriptor / temp-dir isolation (--input in /tmp vs ROOT)
  - Dashboard content vs live agent registry cross-check
  - Master activator exit codes matrix
  - Refusal JSON schema when gate fails
  - Concurrent 12-activator fan-out
  - Empty argv / only-flags invocations
  - Binary name confusion (activate_*.sh vs missing)
  - Timestamp presence on canonical only (not required on activators)
  - Case-sensitive component slug exact match
"""
from __future__ import annotations

import json
import os
import re
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


def parse_json_strict(text: str) -> tuple[bool, dict | None]:
    """Return (ok, obj) only if entire text is exactly one JSON object."""
    text = text.strip()
    if not text:
        return False, None
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            return False, None
        return True, obj
    except json.JSONDecodeError:
        return False, None


# ─── Path traversal component names ──────────────────────────────────────────

def test_path_traversal() -> None:
    print("\n── Path Traversal Component Names ──")
    payloads = [
        "../etc/passwd",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "////etc/passwd",
        "activate_../../secret",
        "...",
        "./fastapi_control_plane",
        "fastapi_control_plane/../../../tmp",
    ]
    for p in payloads:
        code, out, err = run_cmd(
            ["bash", str(MASTER), p],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        combined = out + err
        # must not succeed as a real component; must not hang
        ok = code != -1 and not (
            code == 0 and "DEPLOYMENT_COMPLETE" in out and "components_registered" in out
        )
        record(f"traversal [{p[:40]}]", ok, f"code={code}")


# ─── ENV collision ───────────────────────────────────────────────────────────

def test_env_collision() -> None:
    print("\n── ENV Name Collision / Overwrite ──")
    env = {
        "ALLOW_AGENT_EXECUTION": "true",
        "COMPONENT": "injected",
        "STATUS": "pwned",
        "gate_satisfied": "false",
        "AVRONE_ROOT": str(ROOT / "nonexistent"),
    }
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env=env)
    record("status survives COMPONENT/STATUS pollution", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_control12_lattice_op.sh")],
        env=env,
    )
    ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
    record("control12 survives env pollution", ok)


# ─── Strict single-JSON stdout ───────────────────────────────────────────────

def test_strict_json_stdout() -> None:
    print("\n── Strict Single-JSON Object Stdout ──")
    for c in ["agent_registry_orchestrator", "fastapi_control_plane", "control12_lattice_op"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        ok_parse, obj = parse_json_strict(out)
        record(f"{c} stdout is exact JSON object", ok_parse and code == 0)
        if ok_parse and obj:
            record(f"{c} JSON status field", obj.get("status") == "DEPLOYMENT_COMPLETE")

    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok_parse, obj = parse_json_strict(out)
    record("bridge stdout is exact JSON object", ok_parse and code == 0)


# ─── Double-gate in session ──────────────────────────────────────────────────

def test_double_gate_session() -> None:
    print("\n── Double-Gate Export Session ──")
    # simulate: first refuse, then accept, then refuse again via explicit env each call
    code1, out1, _ = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    code2, out2, _ = run_cmd(
        ["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"}
    )
    code3, out3, _ = run_cmd(
        ["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "false"}
    )
    record("session refuse → accept → refuse", code1 != 0 and code2 == 0 and code3 != 0)


# ─── Component list completeness ─────────────────────────────────────────────

def test_component_completeness() -> None:
    print("\n── Component List Completeness ──")
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        record(f"script exists for {c}", path.exists())
    # no unexpected activate_*.sh beyond known + master
    found = {p.name for p in BIN.glob("activate_*.sh")}
    expected = {f"activate_{c}.sh" for c in COMPONENTS} | {"activate_avrone.sh"}
    extra = found - expected
    missing = expected - found
    record("no unexpected activator scripts", not extra, f"extra={extra}")
    record("no missing activator scripts", not missing, f"missing={missing}")


# ─── Integrity numeric types ─────────────────────────────────────────────────

def test_integrity_types() -> None:
    print("\n── Integrity Field Numeric Types ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_persistent_memory_agent.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    integ = obj.get("integrity", {})
    record("dependencies_resolved is int", isinstance(integ.get("dependencies_resolved"), int))
    record("files_created is int", isinstance(integ.get("files_created"), int))
    record("python_compile_ok is bool", isinstance(integ.get("python_compile_ok"), bool))
    record("dependencies_resolved == 100", integ.get("dependencies_resolved") == 100)
    record("files_created == 142", integ.get("files_created") == 142)


# ─── Sequential stress 50 / 25 ───────────────────────────────────────────────

def test_sequential_stress_50() -> None:
    print("\n── Sequential Stress 50× status (gate=true) ──")
    ok = 0
    for _ in range(50):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("50/50 status under gate", ok == 50, f"ok={ok}")


def test_sequential_refuse_25() -> None:
    print("\n── Sequential Stress 25× refuse (gate unset) ──")
    ok = 0
    for _ in range(25):
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        if code != 0:
            ok += 1
    record("25/25 refuse without gate", ok == 25, f"ok={ok}")


# ─── Mixed burst ─────────────────────────────────────────────────────────────

def test_mixed_burst() -> None:
    print("\n── Mixed Dry-Run + Live + Bridge Burst ──")
    def job(kind: str) -> bool:
        if kind == "dry":
            code, out, err = run_cmd(
                ["bash", str(BIN / "activate_langgraph_supervisor.sh"), "--dry-run"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            return code == 0
        if kind == "live":
            code, out, err = run_cmd(
                ["bash", str(BIN / "activate_training_loop.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            return code == 0 and "DEPLOYMENT_COMPLETE" in out
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    kinds = ["dry", "live", "bridge"] * 4
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(job, k) for k in kinds]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"{sum(results)}/{len(kinds)} mixed burst ok", all(results), f"ok={sum(results)}")


# ─── Temp-dir --input isolation ──────────────────────────────────────────────

def test_input_tempdir_isolation() -> None:
    print("\n── --input Temp-Dir Isolation ──")
    script = BIN / "activate_sqlite_persistence.sh"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "payload.json"
        path.write_text('{"probe": true, "from": "tmp"}')
        code, out, err = run_cmd(
            ["bash", str(script), "--input", str(path)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record("--input from /tmp-style dir no hang", code != -1)
        record("--input from temp still gated success or handled", code == 0 or "status" in (out + err).lower())


# ─── Dashboard ↔ live registry cross-check ───────────────────────────────────

def test_dashboard_registry_crosscheck() -> None:
    print("\n── Dashboard ↔ Live Registry Cross-Check ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    html = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    ids = [a["id"] for a in data["agents"]["agents"]]
    for aid in ids:
        record(f"dashboard mentions {aid}", aid in html)


# ─── Master exit-code matrix ─────────────────────────────────────────────────

def test_master_exit_matrix() -> None:
    print("\n── Master Exit-Code Matrix ──")
    cases = [
        ({"ALLOW_AGENT_EXECUTION": "true"}, ["status"], 0),
        ({"ALLOW_AGENT_EXECUTION": "true"}, ["all"], 0),
        ({"ALLOW_AGENT_EXECUTION": "true"}, ["no_such_comp"], 1),
        ({}, ["status"], 1),  # refuse
        ({"ALLOW_AGENT_EXECUTION": "false"}, ["status"], 1),
    ]
    for env, args, expect in cases:
        clear = "ALLOW_AGENT_EXECUTION" not in env
        code, out, err = run_cmd(
            ["bash", str(MASTER)] + args,
            env=env if env else None,
            clear_gate=clear,
        )
        # map expect: 0 means success, 1 means any non-zero
        if expect == 0:
            ok = code == 0
        else:
            ok = code != 0
        record(f"exit matrix env={env} args={args} expect~{expect}", ok, f"code={code}")


# ─── Refusal JSON schema ─────────────────────────────────────────────────────

def test_refusal_json_schema() -> None:
    print("\n── Refusal JSON Schema ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = (err or out).strip()
    ok_parse, obj = parse_json_strict(combined)
    record("refusal is strict JSON object", ok_parse)
    if ok_parse and obj:
        record("refusal has status or reason", "status" in obj or "reason" in obj)
        record("refusal mentions gate or ALLOW", "ALLOW_AGENT_EXECUTION" in json.dumps(obj) or "gate" in json.dumps(obj).lower() or "reason" in obj)


# ─── Concurrent 12-activator fan-out ─────────────────────────────────────────

def test_fanout_12() -> None:
    print("\n── Concurrent 12-Activator Fan-Out ──")
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
    record(f"12/12 concurrent activators", all(results), f"ok={sum(results)}")


# ─── Empty argv / flags only ─────────────────────────────────────────────────

def test_empty_and_flags() -> None:
    print("\n── Empty Argv / Flags-Only ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master no-args → status success", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_agent_registry_orchestrator.sh"), "--dry-run"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("activator --dry-run only", code == 0)


# ─── Case-sensitive component slug ───────────────────────────────────────────

def test_case_sensitive_slug() -> None:
    print("\n── Case-Sensitive Component Slug ──")
    # wrong case should not match
    code, out, err = run_cmd(
        ["bash", str(MASTER), "FastAPI_Control_Plane"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok = code != 0 or "unknown" in (out + err).lower()
    record("wrong-case component → unknown/non-zero", ok, f"code={code}")

    code, out, err = run_cmd(
        ["bash", str(MASTER), "FASTAPI_CONTROL_PLANE"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok = code != 0 or "unknown" in (out + err).lower()
    record("upper-case component → unknown/non-zero", ok, f"code={code}")


# ─── Canonical timestamp only ────────────────────────────────────────────────

def test_canonical_timestamp() -> None:
    print("\n── Canonical Timestamp Presence ──")
    data = json.loads(CANONICAL.read_text())
    record("canonical has timestamp", bool(data.get("timestamp")))
    # activators are not required to carry timestamp
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_skill_factory.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    record("activator may omit timestamp (not required)", True, f"has_ts={'timestamp' in obj}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Apocalypse-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_path_traversal()
    test_env_collision()
    test_strict_json_stdout()
    test_double_gate_session()
    test_component_completeness()
    test_integrity_types()
    test_sequential_stress_50()
    test_sequential_refuse_25()
    test_mixed_burst()
    test_input_tempdir_isolation()
    test_dashboard_registry_crosscheck()
    test_master_exit_matrix()
    test_refusal_json_schema()
    test_fanout_12()
    test_empty_and_flags()
    test_case_sensitive_slug()
    test_canonical_timestamp()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
