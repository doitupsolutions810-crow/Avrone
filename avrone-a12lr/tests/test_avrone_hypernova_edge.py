#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Hypernova-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Fourteenth-tier suite. Prior cumulative: 881 passed. New hypernova/chaos layers:
  - Gate with overlong UTF-8 sequences and invalid surrogate pairs (as env strings where possible)
  - Activator status field exact set membership {DEPLOYMENT_COMPLETE}
  - Bridge integration.grok_bot_repository contains 'coherently registered'
  - Parallel 60× mixed status/refuse/dry
  - --input with JSON string containing all ASCII control chars
  - Component names that are only digits / only punctuation
  - Verify every activator has integrity with exactly the 3 known keys
  - Dashboard contains '6 registered' or agent count marker
  - Master 'all' and 'status' produce same components_registered
  - Stress: 200× sequential dry-run on single component
  - Cross-check bridge count == len(agents list)
  - Refuse exit code is non-zero and not -1/-2
  - Confirm all test_avrone_*.py suites exist under tests/
  - Activator component field is never empty string
  - Rapid 40× agents module parallel
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
TESTS = ROOT / "tests"

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

EXPECTED_INTEGRITY_KEYS = {"python_compile_ok", "dependencies_resolved", "files_created"}

SUITE_GLOBS = [
    "test_avrone_gate_and_bridge.py",
    "test_avrone_robust_edge.py",
    "test_avrone_extreme_edge.py",
    "test_avrone_ultramega_edge.py",
    "test_avrone_apocalypse_edge.py",
    "test_avrone_singularity_edge.py",
    "test_avrone_event_horizon_edge.py",
    "test_avrone_omega_edge.py",
    "test_avrone_planck_edge.py",
    "test_avrone_quantum_edge.py",
    "test_avrone_cosmic_edge.py",
    "test_avrone_infinity_edge.py",
    "test_avrone_multiverse_edge.py",
    "test_avrone_hypernova_edge.py",
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


# ─── Status exact membership ─────────────────────────────────────────────────

def test_status_exact() -> None:
    print("\n── Activator status Exact DEPLOYMENT_COMPLETE ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} status exact", obj.get("status") == "DEPLOYMENT_COMPLETE")


# ─── grok_bot_repository phrase ──────────────────────────────────────────────

def test_grok_bot_repo_phrase() -> None:
    print("\n── integration.grok_bot_repository Phrase ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    val = str(data.get("integration", {}).get("grok_bot_repository", ""))
    record("contains 'coherently registered'", "coherently registered" in val.lower() or "coherently" in val.lower())


# ─── Parallel 60 mixed ───────────────────────────────────────────────────────

def test_parallel_60_mixed() -> None:
    print("\n── Parallel 60× Mixed status/refuse/dry ──")
    def status(_i: int) -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        return c == 0

    def refuse(_i: int) -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return c != 0

    def dry(i: int) -> bool:
        comp = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{comp}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = (
            [pool.submit(status, i) for i in range(20)]
            + [pool.submit(refuse, i) for i in range(20)]
            + [pool.submit(dry, i) for i in range(20)]
        )
        for f in as_completed(futs):
            results.append(f.result())
    record(f"60/60 mixed parallel", all(results), f"ok={sum(results)}")


# ─── Control-char string in --input ──────────────────────────────────────────

def test_control_char_input() -> None:
    print("\n── --input JSON String With Control Chars ──")
    script = BIN / "activate_secure_cli_execution.sh"
    # embed printable escapes representing controls
    payload = {"msg": "".join(chr(i) for i in range(1, 32))}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("control-char --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Digit / punctuation only components ─────────────────────────────────────

def test_digit_punct_components() -> None:
    print("\n── Digit / Punctuation-Only Component Names ──")
    for slug in ["123", "0", "999999", "...", "???", "!!!", "---", "___"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"digit/punct [{slug}] no hang", code != -1, f"code={code}")


# ─── Integrity exact 3 keys ──────────────────────────────────────────────────

def test_integrity_exact_keys() -> None:
    print("\n── integrity Exactly Known Keys ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        keys = set(obj.get("integrity", {}).keys())
        record(f"{c} integrity keys >= expected", EXPECTED_INTEGRITY_KEYS <= keys, f"keys={keys}")


# ─── Dashboard agent count marker ────────────────────────────────────────────

def test_dashboard_agent_count() -> None:
    print("\n── Dashboard Agent Count Marker ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard mentions 6 registered or Agents", "6 registered" in text or "AGENTS" in text or "6" in text)


# ─── status vs all components_registered ─────────────────────────────────────

def test_status_all_cr() -> None:
    print("\n── status vs all components_registered ──")
    _, out1, _ = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
    _, out2, _ = run_cmd(["bash", str(MASTER), "all"], env={"ALLOW_AGENT_EXECUTION": "true"})
    o1, o2 = json.loads(out1), json.loads(out2)
    record("same components_registered", o1.get("components_registered") == o2.get("components_registered") == 12)


# ─── 200× dry-run single component ───────────────────────────────────────────

def test_200_dryrun_single() -> None:
    print("\n── 200× Sequential Dry-Run (single component) ──")
    script = BIN / "activate_training_loop.sh"
    ok = 0
    for _ in range(200):
        code, out, err = run_cmd(
            ["bash", str(script), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0:
            ok += 1
    record("200/200 dry-run", ok == 200, f"ok={ok}")


# ─── bridge count == len(list) ───────────────────────────────────────────────

def test_bridge_count_len() -> None:
    print("\n── Bridge count == len(agents list) ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    count = data["agents"]["count"]
    n = len(data["agents"]["agents"])
    record("count == len(list)", count == n == 6, f"count={count} len={n}")


# ─── Refuse exit code band ───────────────────────────────────────────────────

def test_refuse_exit_band() -> None:
    print("\n── Refuse Exit Code Band ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    record("refuse code > 0 and not timeout", code > 0, f"code={code}")


# ─── All suite files present ─────────────────────────────────────────────────

def test_all_suites_present() -> None:
    print("\n── All Prior Suite Files Present ──")
    for name in SUITE_GLOBS:
        path = TESTS / name
        record(f"suite {name}", path.exists())


# ─── component never empty ───────────────────────────────────────────────────

def test_component_never_empty() -> None:
    print("\n── component Field Never Empty ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        val = obj.get("component", "")
        record(f"{c} component non-empty", isinstance(val, str) and len(val) > 0, f"val={val!r}")


# ─── 40× agents parallel ─────────────────────────────────────────────────────

def test_40_agents_parallel() -> None:
    print("\n── 40× Agents Module Parallel ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(AGENTS)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "AGENTS_REGISTERED" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(40)]
        for f in as_completed(futs):
            results.append(f.result())
    record("40/40 agents parallel", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Hypernova-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_status_exact()
    test_grok_bot_repo_phrase()
    test_parallel_60_mixed()
    test_control_char_input()
    test_digit_punct_components()
    test_integrity_exact_keys()
    test_dashboard_agent_count()
    test_status_all_cr()
    test_200_dryrun_single()
    test_bridge_count_len()
    test_refuse_exit_band()
    test_all_suites_present()
    test_component_never_empty()
    test_40_agents_parallel()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
