#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime verification suite.
Control704 VIP proxy X Y Z data set code override path.
Validates gate enforcement, activator integrity, agent registry, and bridge output
against canonical kg.production-training-result/v2.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
CANONICAL = ROOT / "references" / "canonical-deployment.json"
AGENTS = ROOT / "agents" / "grok_bot_integration.py"

PASS = 0
FAIL = 0


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def run_cmd(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def test_canonical_exists() -> None:
    ok = CANONICAL.exists()
    record("canonical-deployment.json exists", ok)
    if ok:
        data = json.loads(CANONICAL.read_text())
        record("canonical schema is kg.production-training-result/v2", data.get("schema") == "kg.production-training-result/v2")
        record("canonical status is DEPLOYMENT_COMPLETE", data.get("status") == "DEPLOYMENT_COMPLETE")


def test_gate_refusal() -> None:
    # Without ALLOW_AGENT_EXECUTION=true the master activator must refuse
    code, out, err = run_cmd(["bash", str(BIN / "activate_avrone.sh"), "status"], env={"ALLOW_AGENT_EXECUTION": ""})
    refused = code != 0 and ("refused" in err.lower() or "ALLOW_AGENT_EXECUTION" in err)
    record("gate refuses when ALLOW_AGENT_EXECUTION is unset", refused, f"code={code}")


def test_gate_accepts() -> None:
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_avrone.sh"), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
    record("gate accepts when ALLOW_AGENT_EXECUTION=true", ok, out[:120] if out else err)


def test_component_activators() -> None:
    components = [
        "agent_registry_orchestrator",
        "control12_lattice_op",
        "fastapi_control_plane",
        "persistent_memory_agent",
        "langgraph_supervisor",
    ]
    for c in components:
        script = BIN / f"activate_{c}.sh"
        if not script.exists():
            record(f"activator {c} exists", False)
            continue
        code, out, err = run_cmd(
            ["bash", str(script)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        ok = code == 0 and "DEPLOYMENT_COMPLETE" in out and c in out
        record(f"activator {c}", ok)


def test_bridge_output() -> None:
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    if code != 0:
        record("bridge conclude exits 0", False, err)
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        record("bridge emits valid JSON", False, str(e))
        return
    record("bridge emits valid JSON", True)
    record("bridge status DEPLOYMENT_COMPLETE", data.get("status") == "DEPLOYMENT_COMPLETE")
    record("bridge control704 present", "control704" in data)
    agents = data.get("agents", {})
    record("bridge agents registered", agents.get("status") == "AGENTS_REGISTERED")
    record("bridge gate_satisfied true", agents.get("gate_satisfied") is True)
    count = agents.get("count", 0)
    record("bridge agent count >= 6", count >= 6, f"count={count}")


def test_agent_registry_ids() -> None:
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    if code != 0:
        record("grok_bot_integration runs", False, err)
        return
    data = json.loads(out)
    ids = {a["id"] for a in data.get("agents", {}).get("agents", [])}
    expected = {
        "grok-bot-supervisor",
        "grok-bot-computer-use",
        "grok-bot-github-bridge",
        "avrone-fastapi-control",
        "avrone-react-dashboard",
        "persistent-memory-agent",
    }
    missing = expected - ids
    record("required agent ids present", not missing, f"missing={missing}" if missing else "")


def main() -> int:
    print("=== AVRONE-12 Lattice Runtime Test Suite ===")
    print(f"ROOT={ROOT}")
    print(f"ALLOW_AGENT_EXECUTION (test env) controlled per case\n")

    test_canonical_exists()
    test_gate_refusal()
    test_gate_accepts()
    test_component_activators()
    test_bridge_output()
    test_agent_registry_ids()

    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
