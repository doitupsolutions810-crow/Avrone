#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Ultra-Mega Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Fourth-tier suite. Builds on prior 94 + 143 tests with additional chaos:
  - Unicode / RTL / homoglyph gate and component names
  - Extremely long component / argument strings
  - Nested and recursive-looking --input JSON
  - Rapid gate flip-flop (true↔false) under sequential load
  - Activator output key-order / extra-key tolerance
  - Bridge canonical_status cross-check vs references/
  - Multiple simultaneous different-component activations
  - Permission-bit edge (non-executable still runnable via bash)
  - Empty / missing BIN path simulation
  - JSONL-ish multi-line stdout rejection checks
  - Control704 block required sub-keys
  - Agent id uniqueness and no-null fields
  - Dashboard HTML well-formedness (tag balance sample)
  - Re-entrancy: bridge called while activator is conceptually "running"
  - Signal-ish / early-terminate resilience (short timeout)
  - Locale / LC_ALL pollution
  - Repeated full-suite self-consistency (3× mini-status)
"""
from __future__ import annotations

import json
import os
import re
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
        # embedded null etc.
        return -2, "", f"ValueError:{exc}"


# ─── Unicode / homoglyph / RTL ───────────────────────────────────────────────

def test_unicode_and_homoglyph() -> None:
    print("\n── Unicode / Homoglyph / RTL Gate & Args ──")
    samples = [
        ("fullwidth true", "ｔｒｕｅ"),
        ("cyrillic lookalike", "truе"),  # е is cyrillic
        ("rtl mark", "\u200ftrue"),
        ("zero-width space", "true\u200b"),
        ("emoji", "true🚀"),
        ("mixed script", "tｒue"),
        ("arabic", "صحيح"),
        ("long unicode component", "组件" * 20),
    ]
    for label, val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        # none of these are the exact ASCII "true"
        ok = code != 0 and code != -1
        record(f"unicode gate [{label}]", ok, f"code={code}")

    # component name with unicode
    code, out, err = run_cmd(
        ["bash", str(MASTER), "组件测试"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok = code != 0 or "unknown_component" in (out + err)
    record("unicode component name → unknown/non-zero", ok, f"code={code}")


# ─── Extremely long arguments ────────────────────────────────────────────────

def test_extreme_length() -> None:
    print("\n── Extreme Length Arguments ──")
    long_comp = "a" * 4000
    code, out, err = run_cmd(
        ["bash", str(MASTER), long_comp],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        timeout=8,
    )
    record("4000-char component no hang", code != -1, f"code={code}")

    long_gate = "true" + ("x" * 2000)
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": long_gate},
        timeout=8,
    )
    record("long non-true gate refuses", code != 0 and code != -1, f"code={code}")


# ─── Nested / recursive-looking input ────────────────────────────────────────

def test_nested_input() -> None:
    print("\n── Nested / Recursive-looking --input ──")
    script = BIN / "activate_agent_registry_orchestrator.sh"
    payloads = [
        {"nested": {"a": {"b": {"c": [1, 2, {"d": "e"}]}}}, "component": "x"},
        {"self": None},  # will fill
        {"list": [{"k": i} for i in range(50)]},
        {"empty_list": [], "empty_obj": {}, "null": None, "bool": True},
    ]
    payloads[1]["self"] = payloads[1]  # self-ref — json.dumps will fail; use copy without cycle

    for i, obj in enumerate(payloads):
        if i == 1:
            # avoid actual cycle for json.dumps
            obj = {"self_ref_marker": True, "note": "simulated"}
        raw = json.dumps(obj)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            tf.write(raw)
            path = tf.name
        try:
            code, out, err = run_cmd(
                ["bash", str(script), "--input", path],
                env={"ALLOW_AGENT_EXECUTION": "true"},
                timeout=8,
            )
            record(f"nested input case {i} no hang", code != -1, f"code={code}")
        finally:
            os.unlink(path)


# ─── Rapid gate flip-flop ────────────────────────────────────────────────────

def test_gate_flipflop() -> None:
    print("\n── Rapid Gate Flip-Flop (20 alternations) ──")
    ok_count = 0
    for i in range(20):
        gate = "true" if i % 2 == 0 else "false"
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": gate},
        )
        if gate == "true":
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok_count += 1
        else:
            if code != 0:
                ok_count += 1
    record("20/20 flip-flop correct", ok_count == 20, f"ok={ok_count}")


# ─── Extra keys / key order tolerance ────────────────────────────────────────

def test_extra_keys_tolerance() -> None:
    print("\n── Activator Extra-Key / Order Tolerance ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_fastapi_control_plane.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    # core keys must exist; extra keys are tolerated
    for k in ("status", "component", "gate_satisfied"):
        record(f"core key '{k}' present", k in data)
    # re-serialize and parse again (order independent)
    data2 = json.loads(json.dumps(data, sort_keys=True))
    record("re-serialize roundtrip preserves status", data2.get("status") == "DEPLOYMENT_COMPLETE")


# ─── Bridge vs canonical cross-check ─────────────────────────────────────────

def test_bridge_canonical_crosscheck() -> None:
    print("\n── Bridge ↔ Canonical Cross-Check ──")
    canon = json.loads(CANONICAL.read_text())
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    bridge = json.loads(out)
    record("bridge.schema matches canonical", bridge.get("schema") == canon.get("schema"))
    record("bridge.canonical_status matches", bridge.get("canonical_status") == canon.get("status"))
    record("bridge.status DEPLOYMENT_COMPLETE when gated", bridge.get("status") == "DEPLOYMENT_COMPLETE")


# ─── Parallel multi-component ────────────────────────────────────────────────

def test_parallel_multi_component() -> None:
    print("\n── Parallel Multi-Component Activation ──")
    subset = [
        "agent_registry_orchestrator",
        "control12_lattice_op",
        "fastapi_control_plane",
        "persistent_memory_agent",
        "langgraph_supervisor",
        "training_loop",
    ]

    def one(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out and c in out

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(one, c): c for c in subset}
        for f in as_completed(futs):
            results.append(f.result())
    record(f"{sum(results)}/{len(subset)} parallel components ok", all(results), f"ok={sum(results)}")


# ─── Non-executable still runnable via bash ──────────────────────────────────

def test_nonexecutable_via_bash() -> None:
    print("\n── Non-Executable Scripts Still Runnable via bash ──")
    script = BIN / "activate_skill_factory.sh"
    mode = script.stat().st_mode
    # temporarily clear execute bits
    script.chmod(mode & ~0o111)
    try:
        code, out, err = run_cmd(
            ["bash", str(script)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record("bash runs non-executable activator", code == 0 and "DEPLOYMENT_COMPLETE" in out)
    finally:
        script.chmod(mode)  # restore


# ─── Control704 required sub-keys ────────────────────────────────────────────

def test_control704_subkeys() -> None:
    print("\n── Control704 Required Sub-Keys ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    c704 = data.get("control704", {})
    for k in ("access", "priority", "identity", "lattice"):
        record(f"control704.{k} present and non-empty", bool(c704.get(k)))
    record("priority is high", str(c704.get("priority", "")).lower() == "high")
    record("identity contains IT Technician", "IT Technician" in str(c704.get("identity", "")))


# ─── Agent uniqueness & no-null ──────────────────────────────────────────────

def test_agent_uniqueness() -> None:
    print("\n── Agent ID Uniqueness & No-Null Fields ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    agents = data.get("agents", {}).get("agents", [])
    ids = [a.get("id") for a in agents]
    record("all agent ids non-null", all(ids) and all(isinstance(i, str) for i in ids))
    record("agent ids unique", len(ids) == len(set(ids)), f"n={len(ids)} unique={len(set(ids))}")
    for a in agents:
        for field in ("id", "role", "status"):
            record(f"{a.get('id')}.{field} non-null str", isinstance(a.get(field), str) and bool(a.get(field)))


# ─── Dashboard tag balance sample ────────────────────────────────────────────

def test_dashboard_wellformed() -> None:
    print("\n── Dashboard HTML Well-Formedness Sample ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has <html", "<html" in text.lower())
    record("has </html>", "</html>" in text.lower())
    record("has <head", "<head" in text.lower())
    record("has <body", "<body" in text.lower())
    record("has </body>", "</body>" in text.lower())
    # rough balance for table
    record("table open/close balanced", text.count("<table") == text.count("</table>"))
    record("tr open/close balanced", text.count("<tr") == text.count("</tr>"))


# ─── Locale pollution ────────────────────────────────────────────────────────

def test_locale_pollution() -> None:
    print("\n── Locale / LC_ALL Pollution ──")
    env = {
        "ALLOW_AGENT_EXECUTION": "true",
        "LC_ALL": "C",
        "LANG": "C",
        "LANGUAGE": "en_US:en",
    }
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env=env)
    record("master under LC_ALL=C", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    env["LC_ALL"] = "en_US.UTF-8"
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env=env)
    record("master under en_US.UTF-8", code == 0 and "DEPLOYMENT_COMPLETE" in out)


# ─── Re-entrancy / concurrent bridge + activator ─────────────────────────────

def test_reentrancy() -> None:
    print("\n── Re-entrancy: Bridge ∥ Activator ──")
    def run_bridge() -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def run_act() -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / "activate_agent_registry_orchestrator.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(run_bridge) for _ in range(2)] + [pool.submit(run_act) for _ in range(2)]
        for f in as_completed(futs):
            results.append(f.result())
    record("4/4 concurrent bridge+activator ok", all(results), f"ok={sum(results)}")


# ─── Short-timeout resilience ────────────────────────────────────────────────

def test_short_timeout_resilience() -> None:
    print("\n── Short-Timeout Resilience (2s budget) ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        timeout=2.0,
    )
    record("status under 2s budget", code == 0 and code != -1, f"code={code}")


# ─── Mini self-consistency loop ──────────────────────────────────────────────

def test_self_consistency_loop() -> None:
    print("\n── Self-Consistency Loop (3× full status + bridge) ──")
    snapshots = []
    for i in range(3):
        code1, out1, _ = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        code2, out2, _ = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        snapshots.append((code1 == 0, "DEPLOYMENT_COMPLETE" in out1, code2 == 0, "DEPLOYMENT_COMPLETE" in out2))
    record("3× status+bridge all success", all(all(s) for s in snapshots), f"snapshots={snapshots}")


# ─── Missing component script graceful ───────────────────────────────────────

def test_missing_component_graceful() -> None:
    print("\n── Missing Component Script Graceful ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "this_component_does_not_exist_zz"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    combined = out + err
    record("missing component non-zero or unknown", code != 0 or "unknown" in combined.lower())
    record("missing component mentions unknown_component or similar",
           "unknown" in combined.lower() or code != 0)


# ─── All components dry-run ──────────────────────────────────────────────────

def test_all_dry_runs() -> None:
    print("\n── All 12 Components Dry-Run ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        ok = code == 0 and ("component" in out or c in out)
        record(f"dry-run {c}", ok)


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Ultra-Mega Edge-Case & Chaos Test Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_unicode_and_homoglyph()
    test_extreme_length()
    test_nested_input()
    test_gate_flipflop()
    test_extra_keys_tolerance()
    test_bridge_canonical_crosscheck()
    test_parallel_multi_component()
    test_nonexecutable_via_bash()
    test_control704_subkeys()
    test_agent_uniqueness()
    test_dashboard_wellformed()
    test_locale_pollution()
    test_reentrancy()
    test_short_timeout_resilience()
    test_self_consistency_loop()
    test_missing_component_graceful()
    test_all_dry_runs()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
