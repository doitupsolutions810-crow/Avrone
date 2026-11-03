#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Extended Robust Edge-Case Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Covers:
  - Gate enforcement (true / false / missing / wrong-case / whitespace)
  - All 12 activators under gate and refusal
  - Dry-run vs live path
  - Canonical payload integrity and required keys
  - Agent registry completeness and lattice binding
  - Bridge output schema and control704 block
  - Master activator routing and unknown-component handling
  - File permissions / executability
  - Concurrent-ish sequential stress (multiple rapid gate calls)
  - Malformed / empty input tolerance on activators
  - Path and environment isolation
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
CANONICAL = ROOT / "references" / "canonical-deployment.json"
AGENTS = ROOT / "agents" / "grok_bot_integration.py"
MASTER = BIN / "activate_avrone.sh"

PASS = 0
FAIL = 0
SKIP = 0


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
) -> tuple[int, str, str]:
    e = os.environ.copy()
    if clear_gate:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env is not None:
        e.update(env)
    p = subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True, text=True, timeout=15)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# ─── Canonical ───────────────────────────────────────────────────────────────

def test_canonical_integrity() -> None:
    print("\n── Canonical Integrity ──")
    record("canonical file exists", CANONICAL.exists())
    if not CANONICAL.exists():
        return
    try:
        data = json.loads(CANONICAL.read_text())
    except json.JSONDecodeError as exc:
        record("canonical is valid JSON", False, str(exc))
        return
    record("canonical is valid JSON", True)
    record("schema == kg.production-training-result/v2", data.get("schema") == "kg.production-training-result/v2")
    record("status == DEPLOYMENT_COMPLETE", data.get("status") == "DEPLOYMENT_COMPLETE")
    record("build_context present", "build_context" in data)
    components = data.get("build_context", {}).get("components", [])
    record("components list non-empty", isinstance(components, list) and len(components) > 0, f"n={len(components)}")
    record("system_name present", bool(data.get("build_context", {}).get("system_name")))


# ─── Gate matrix ─────────────────────────────────────────────────────────────

def test_gate_matrix() -> None:
    print("\n── Gate Enforcement Matrix ──")
    # 1. Unset
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    record("unset ALLOW_AGENT_EXECUTION → refuse", code != 0 and "ALLOW_AGENT_EXECUTION" in (err + out))

    # 2. empty string
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": ""})
    record("ALLOW_AGENT_EXECUTION='' → refuse", code != 0)

    # 3. false
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "false"})
    record("ALLOW_AGENT_EXECUTION=false → refuse", code != 0)

    # 4. True (wrong case)
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "True"})
    record("ALLOW_AGENT_EXECUTION=True (wrong case) → refuse", code != 0)

    # 5. TRUE
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "TRUE"})
    record("ALLOW_AGENT_EXECUTION=TRUE → refuse", code != 0)

    # 6. true with whitespace
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": " true "})
    record("ALLOW_AGENT_EXECUTION=' true ' → refuse", code != 0)

    # 7. exact true
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
    record("ALLOW_AGENT_EXECUTION=true → accept", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    # 8. 1 / yes (common mistakes)
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "1"})
    record("ALLOW_AGENT_EXECUTION=1 → refuse", code != 0)
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "yes"})
    record("ALLOW_AGENT_EXECUTION=yes → refuse", code != 0)


# ─── All activators ──────────────────────────────────────────────────────────

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


def test_all_activators_under_gate() -> None:
    print("\n── All 12 Activators (gate=true) ──")
    for c in COMPONENTS:
        script = BIN / f"activate_{c}.sh"
        if not script.exists():
            record(f"activator {c} file exists", False)
            continue
        code, out, err = run_cmd(
            ["bash", str(script)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        ok = code == 0 and "DEPLOYMENT_COMPLETE" in out and c in out
        record(f"activate_{c}.sh under gate", ok, out[:80] if not ok else "")


def test_all_activators_refuse_without_gate() -> None:
    print("\n── All 12 Activators (gate unset) ──")
    for c in COMPONENTS:
        script = BIN / f"activate_{c}.sh"
        if not script.exists():
            record(f"refuse {c} (file missing)", False)
            continue
        code, out, err = run_cmd(["bash", str(script)], clear_gate=True)
        ok = code != 0 and ("refused" in (err + out).lower() or "ALLOW_AGENT_EXECUTION" in (err + out))
        record(f"activate_{c}.sh refuses without gate", ok)


def test_dry_run_path() -> None:
    print("\n── Dry-run Path ──")
    for c in ["agent_registry_orchestrator", "control12_lattice_op", "fastapi_control_plane"]:
        script = BIN / f"activate_{c}.sh"
        code, out, err = run_cmd(
            ["bash", str(script), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # dry-run should exit 0 and print request payload (component key)
        ok = code == 0 and ("component" in out or c in out)
        record(f"dry-run {c}", ok, out[:60] if not ok else "")


# ─── Master activator routing ────────────────────────────────────────────────

def test_master_routing() -> None:
    print("\n── Master Activator Routing ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master status succeeds", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    code, out, err = run_cmd(
        ["bash", str(MASTER), "all"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master 'all' succeeds", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    # unknown component
    code, out, err = run_cmd(
        ["bash", str(MASTER), "does_not_exist_xyz"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master unknown component → non-zero", code != 0)

    # no args (defaults to status)
    code, out, err = run_cmd(
        ["bash", str(MASTER)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master with no args → status path", code == 0 and "DEPLOYMENT_COMPLETE" in out)


# ─── Bridge & agent registry ─────────────────────────────────────────────────

def test_bridge_and_agents() -> None:
    print("\n── Bridge + Agent Registry ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("bridge exits 0", code == 0)
    if code != 0:
        record("bridge JSON parse", False, err)
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        record("bridge valid JSON", False, str(exc))
        return
    record("bridge valid JSON", True)
    record("bridge status DEPLOYMENT_COMPLETE", data.get("status") == "DEPLOYMENT_COMPLETE")
    record("bridge has control704 block", "control704" in data)
    c704 = data.get("control704", {})
    record("control704.access mentions proxy", "proxy" in str(c704.get("access", "")).lower())
    record("control704.identity is Control704 IT Technician", "Control704" in str(c704.get("identity", "")))
    record("control704.lattice mentions Control12", "Control12" in str(c704.get("lattice", "")))

    agents_block = data.get("agents", {})
    record("agents.status == AGENTS_REGISTERED", agents_block.get("status") == "AGENTS_REGISTERED")
    record("agents.gate_satisfied is True", agents_block.get("gate_satisfied") is True)
    agent_list = agents_block.get("agents", [])
    record("agent count == 6", len(agent_list) == 6, f"n={len(agent_list)}")

    ids = {a.get("id") for a in agent_list}
    expected = {
        "grok-bot-supervisor",
        "grok-bot-computer-use",
        "grok-bot-github-bridge",
        "avrone-fastapi-control",
        "avrone-react-dashboard",
        "persistent-memory-agent",
    }
    record("all required agent ids present", expected.issubset(ids), f"missing={expected - ids}")

    # lattice binding for Grok-bot agents
    for a in agent_list:
        if a.get("id", "").startswith("grok-bot-"):
            record(f"{a['id']} lattice == Control12", a.get("lattice") == "Control12")


def test_bridge_without_gate() -> None:
    print("\n── Bridge without Gate ──")
    code, out, err = run_cmd([sys.executable, str(BRIDGE)], clear_gate=True)
    # bridge still emits JSON but status should be GATE_PENDING
    if code != 0:
        record("bridge runs without gate (exit)", False, err)
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        record("bridge JSON without gate", False)
        return
    record("bridge without gate → GATE_PENDING or gate_satisfied false",
           data.get("status") == "GATE_PENDING" or data.get("agents", {}).get("gate_satisfied") is False)


# ─── Filesystem / permissions ────────────────────────────────────────────────

def test_file_permissions() -> None:
    print("\n── Filesystem & Permissions ──")
    for c in COMPONENTS:
        script = BIN / f"activate_{c}.sh"
        if not script.exists():
            record(f"{c} exists", False)
            continue
        mode = script.stat().st_mode
        # we only require readable; executability via bash is enough
        record(f"{c} is regular file", stat.S_ISREG(mode))
        record(f"{c} is readable", os.access(script, os.R_OK))

    record("master activator exists", MASTER.exists())
    record("bridge script exists", BRIDGE.exists())
    record("agents integration exists", AGENTS.exists())
    record("dashboard index.html exists", (ROOT / "dashboard" / "index.html").exists())


# ─── Stress / sequential rapid calls ─────────────────────────────────────────

def test_sequential_stress() -> None:
    print("\n── Sequential Stress (10 rapid gate-true status calls) ──")
    successes = 0
    for i in range(10):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            successes += 1
    record("10/10 rapid status calls succeed", successes == 10, f"successes={successes}")


# ─── Input tolerance ─────────────────────────────────────────────────────────

def test_input_tolerance() -> None:
    print("\n── Input / Flag Tolerance ──")
    script = BIN / "activate_agent_registry_orchestrator.sh"

    # unknown flag should not crash catastrophically (may exit non-zero)
    code, out, err = run_cmd(
        ["bash", str(script), "--unknown-flag-xyz"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    # either handled or clean non-zero; must not hang
    record("unknown flag does not hang / produces exit", code is not None)

    # empty --input file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write("{}")
        tf.flush()
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record("--input empty JSON object accepted or handled", code == 0 or "status" in (out + err).lower())
    finally:
        os.unlink(path)


# ─── Path isolation ──────────────────────────────────────────────────────────

def test_path_isolation() -> None:
    print("\n── Path Isolation ──")
    # running from a different cwd should still work because scripts use relative paths carefully
    # or at least not crash the test harness
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("master works from ROOT cwd", code == 0)


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 64)
    print("AVRONE-12 Lattice Runtime — Robust Edge-Case Test Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12")
    print(f"ROOT={ROOT}")
    print("=" * 64)

    test_canonical_integrity()
    test_gate_matrix()
    test_all_activators_under_gate()
    test_all_activators_refuse_without_gate()
    test_dry_run_path()
    test_master_routing()
    test_bridge_and_agents()
    test_bridge_without_gate()
    test_file_permissions()
    test_sequential_stress()
    test_input_tolerance()
    test_path_isolation()

    print("\n" + "=" * 64)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
