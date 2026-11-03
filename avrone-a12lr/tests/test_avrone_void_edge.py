#!/usr/bin/env python3
"""AVRONE-12 — Void-level edge suite. Fast, new failure classes only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
MASTER = BIN / "activate_avrone.sh"
CANON = ROOT / "references" / "canonical-deployment.json"
DASH = ROOT / "dashboard" / "index.html"

PASS = FAIL = 0
COMPONENTS = [
    "fastapi_control_plane", "react_dashboard", "openai_model_adapter",
    "agent_registry_orchestrator", "secure_cli_execution", "sqlite_persistence",
    "containerized_deployment", "persistent_memory_agent", "control12_lattice_op",
    "langgraph_supervisor", "training_loop", "skill_factory",
]


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(cmd, env=None, clear=False, timeout=8.0):
    e = os.environ.copy()
    if clear:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def main() -> int:
    print("AVRONE-12 Void-level edge suite")
    # exact true only
    for val in ["true", "true\x00" if False else "truE", "true\u200b", "TRUE", " true", "true "]:
        code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": val})
        rec(f"gate {val!r} {'accept' if val=='true' else 'refuse'}", (code == 0) if val == "true" else code != 0, f"code={code}")

    # null byte rejected by OS
    try:
        run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true\0"})
        rec("null-byte gate rejected", False)
    except ValueError:
        rec("null-byte gate rejected by OS", True)

    code, out, err = run(["bash", str(MASTER), "status"], clear=True)
    rec("unset refuses", code != 0 and "DEPLOYMENT_COMPLETE" not in out)

    for c in COMPONENTS:
        code, out, err = run(["bash", str(BIN / f"activate_{c}.sh")], env={"ALLOW_AGENT_EXECUTION": "true"})
        obj = json.loads(out) if code == 0 else {}
        rec(f"{c} complete", code == 0 and obj.get("status") == "DEPLOYMENT_COMPLETE" and obj.get("component") == c)
        rec(f"{c} gate bool", obj.get("gate_satisfied") is True)
        rec(f"{c} deps int 100", obj.get("integrity", {}).get("dependencies_resolved") == 100)

    code, out, err = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    data = json.loads(out)
    rec("bridge complete", data.get("status") == "DEPLOYMENT_COMPLETE")
    rec("priority high", data.get("control704", {}).get("priority") == "high")
    rec("6 agents", data.get("agents", {}).get("count") == 6)
    rec("schema match", data.get("schema") == json.loads(CANON.read_text())["schema"])

    code, out, err = run([sys.executable, str(BRIDGE)], clear=True)
    pending = json.loads(out)
    rec("ungated GATE_PENDING", pending.get("status") == "GATE_PENDING")
    rec("ungated gate false", pending.get("agents", {}).get("gate_satisfied") is False)

    # traversal must not succeed as status
    for slug in ["../etc/passwd", "status/../../bin", "CON", "true"]:
        code, out, err = run(["bash", str(MASTER), slug], env={"ALLOW_AGENT_EXECUTION": "true"})
        rec(f"slug {slug!r} not status", not (code == 0 and "components_registered" in out), f"code={code}")

    html = DASH.read_text(encoding="utf-8")
    rec("dashboard vip", "VIP" in html and "Control704" in html)
    rec("dashboard agents", html.count("grok-bot-") >= 3)

    # --output writes file
    outp = ROOT / "runs" / "void-out.json"
    code, out, err = run(
        ["bash", str(BIN / "activate_skill_factory.sh"), "--output", str(outp)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("output file written", outp.exists() and "DEPLOYMENT_COMPLETE" in outp.read_text())
    outp.unlink(missing_ok=True)

    print(f"Results: {PASS} passed · {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
