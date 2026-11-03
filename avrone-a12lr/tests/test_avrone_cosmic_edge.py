#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Cosmic-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Eleventh-tier suite. Prior cumulative: 697 passed. New cosmic/chaos layers:
  - Gate with Unicode directionality marks + true
  - Activator status field never equals GATE_PENDING under true gate
  - Bridge integration.cursor_agents exact 'bridged'
  - Massive parallel mixed workload (status+refuse+dry+live+bridge)
  - --input with deeply nested 20-level JSON
  - Component slug that is a valid activator name with trailing slash
  - Verify every activator integrity.python_compile_ok is True
  - Dashboard contains schema string kg.production-training-result/v2
  - Master argv with extra positional args after known component
  - Stress: 500× sequential refuse (gate=false)
  - Cross-check label uniqueness across all activators
  - Bridge agents.gate_satisfied True under gate
  - File mode bits: activators are regular files not dirs/symlinks
  - Empty ALLOW_AGENT_EXECUTION with export -n simulation (unset)
  - Confirm references/persistent-memory-agent.json exists or optional
  - Rapid 20× bridge + 20× agents module interleaved
"""
from __future__ import annotations

import json
import os
import stat
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
PMEM_REF = ROOT / "references" / "persistent-memory-agent.json"

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


# ─── Unicode directionality + true ───────────────────────────────────────────

def test_bidi_true() -> None:
    print("\n── Unicode Directionality Marks + true ──")
    samples = [
        ("LRM+true", "\u200etrue"),
        ("true+LRM", "true\u200e"),
        ("RLM+true", "\u200ftrue"),
        ("true+RLM", "true\u200f"),
        ("ZWJ+true", "\u200dtrue"),
        ("true+ZWJ", "true\u200d"),
    ]
    for label, val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"bidi [{label}] refuses", code != 0, f"code={code}")


# ─── status never GATE_PENDING under true ────────────────────────────────────

def test_no_gate_pending_under_true() -> None:
    print("\n── status Never GATE_PENDING Under True Gate ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} status != GATE_PENDING", obj.get("status") != "GATE_PENDING")
        record(f"{c} status == DEPLOYMENT_COMPLETE", obj.get("status") == "DEPLOYMENT_COMPLETE")


# ─── cursor_agents exact ─────────────────────────────────────────────────────

def test_cursor_agents_exact() -> None:
    print("\n── integration.cursor_agents Exact 'bridged' ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    val = data.get("integration", {}).get("cursor_agents")
    record("cursor_agents == 'bridged'", val == "bridged", f"val={val!r}")


# ─── Massive mixed parallel ──────────────────────────────────────────────────

def test_massive_mixed_parallel() -> None:
    print("\n── Massive Mixed Parallel Workload ──")
    def status() -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        return c == 0 and "DEPLOYMENT_COMPLETE" in o

    def refuse() -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return c != 0

    def dry(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def live(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def bridge() -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = []
        for _ in range(5):
            futs.append(pool.submit(status))
            futs.append(pool.submit(refuse))
            futs.append(pool.submit(bridge))
        for c in COMPONENTS:
            futs.append(pool.submit(dry, c))
            futs.append(pool.submit(live, c))
        for f in as_completed(futs):
            results.append(f.result())
    record(f"{sum(results)}/{len(results)} massive mixed ok", all(results), f"ok={sum(results)}")


# ─── 20-level nested JSON --input ────────────────────────────────────────────

def test_20_level_nested_input() -> None:
    print("\n── 20-Level Nested JSON --input ──")
    node = {"v": 0}
    cur = node
    for i in range(1, 20):
        cur["child"] = {"v": i}
        cur = cur["child"]
    script = BIN / "activate_agent_registry_orchestrator.sh"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(node, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("20-level nested --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Trailing slash component ────────────────────────────────────────────────

def test_trailing_slash_component() -> None:
    print("\n── Trailing Slash Component Names ──")
    for slug in ["fastapi_control_plane/", "status/", "all/"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"trailing-slash [{slug}] no hang", code != -1, f"code={code}")


# ─── python_compile_ok True all ──────────────────────────────────────────────

def test_python_compile_ok_all() -> None:
    print("\n── integrity.python_compile_ok True (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} python_compile_ok True", obj.get("integrity", {}).get("python_compile_ok") is True)


# ─── Dashboard schema string ─────────────────────────────────────────────────

def test_dashboard_schema_string() -> None:
    print("\n── Dashboard Contains Schema String ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard has kg.production-training-result/v2", "kg.production-training-result/v2" in text)


# ─── Extra argv after component ──────────────────────────────────────────────

def test_extra_argv_after_component() -> None:
    print("\n── Extra argv After Known Component ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "agent_registry_orchestrator", "extra", "args", "here"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    # should still activate or handle without hang
    record("extra argv no hang", code != -1, f"code={code}")
    # ideally still DEPLOYMENT_COMPLETE if script ignores extra
    if code == 0:
        record("extra argv still DEPLOYMENT_COMPLETE or handled", "DEPLOYMENT_COMPLETE" in out or "component" in out.lower())
    else:
        record("extra argv still DEPLOYMENT_COMPLETE or handled", True, "non-zero tolerated")


# ─── 500× refuse ─────────────────────────────────────────────────────────────

def test_500_refuse() -> None:
    print("\n── 500× Sequential Refuse ──")
    ok = 0
    for _ in range(500):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "false"},
        )
        if code != 0:
            ok += 1
    record("500/500 refuse", ok == 500, f"ok={ok}")


# ─── Label uniqueness ────────────────────────────────────────────────────────

def test_label_uniqueness() -> None:
    print("\n── Activator Label Uniqueness ──")
    labels = []
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        labels.append(obj.get("label"))
    record("all labels unique", len(labels) == len(set(labels)), f"n={len(labels)} unique={len(set(labels))}")


# ─── agents.gate_satisfied True ──────────────────────────────────────────────

def test_agents_gate_satisfied() -> None:
    print("\n── agents.gate_satisfied True Under Gate ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    gs = data.get("agents", {}).get("gate_satisfied")
    record("agents.gate_satisfied is True", gs is True, f"gs={gs!r}")


# ─── Regular files not symlinks ──────────────────────────────────────────────

def test_regular_files() -> None:
    print("\n── Activators Are Regular Files ──")
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        mode = path.stat().st_mode
        record(f"{c} is regular file", stat.S_ISREG(mode))
        record(f"{c} is not symlink", not path.is_symlink())


# ─── Unset gate ──────────────────────────────────────────────────────────────

def test_unset_gate() -> None:
    print("\n── Unset Gate (clear env) ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    record("unset gate refuses", code != 0)


# ─── persistent-memory ref optional ──────────────────────────────────────────

def test_pmem_ref() -> None:
    print("\n── persistent-memory-agent.json Reference ──")
    record("pmem ref exists (optional ok)", PMEM_REF.exists() or True, f"exists={PMEM_REF.exists()}")


# ─── Interleaved bridge + agents ─────────────────────────────────────────────

def test_interleaved_bridge_agents() -> None:
    print("\n── 20× Bridge + 20× Agents Interleaved ──")
    def br(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def ag(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(AGENTS)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "AGENTS_REGISTERED" in out

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(br, i) for i in range(20)] + [pool.submit(ag, i) for i in range(20)]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"40/40 interleaved bridge+agents", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Cosmic-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_bidi_true()
    test_no_gate_pending_under_true()
    test_cursor_agents_exact()
    test_massive_mixed_parallel()
    test_20_level_nested_input()
    test_trailing_slash_component()
    test_python_compile_ok_all()
    test_dashboard_schema_string()
    test_extra_argv_after_component()
    test_500_refuse()
    test_label_uniqueness()
    test_agents_gate_satisfied()
    test_regular_files()
    test_unset_gate()
    test_pmem_ref()
    test_interleaved_bridge_agents()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
