#!/usr/bin/env python3
"""AVRONE-12 — Abyss-level edge suite.

Covers defaults, type strictness, case sensitivity, umask, and a
small concurrent bridge+status burst. Designed to run beside trench.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
MASTER = BIN / "activate_avrone.sh"
CANON = ROOT / "references" / "canonical-deployment.json"

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
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def main() -> int:
    print("AVRONE-12 Abyss-level edge suite")

    # no args defaults to status
    c, o, e = run(["bash", str(MASTER)], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("no-args is status", c == 0 and "components_registered" in o and e.strip() == "")

    # type strictness
    obj = json.loads(o)
    rec("registered is int", isinstance(obj.get("components_registered"), int))
    rec("registered not str", not isinstance(obj.get("components_registered"), str))
    rec("status is str", isinstance(obj.get("status"), str))
    rec("no nan", not any(isinstance(v, float) and math.isnan(v) for v in obj.values()))

    # case-sensitive slugs
    for bad in ["Skill_Factory", "SKILL_FACTORY", "skill-factory", "skill_factory/"]:
        c, o, e = run(["bash", str(MASTER), bad], env={"ALLOW_AGENT_EXECUTION": "true"})
        rec(f"case {bad!r} unknown", c != 0 and "components_registered" not in o)

    # umask does not corrupt JSON
    old = os.umask(0o077)
    try:
        c, o, e = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        rec("umask 077 status", c == 0 and json.loads(o).get("status") == "DEPLOYMENT_COMPLETE")
    finally:
        os.umask(old)

    # double dry-run flag
    script = BIN / "activate_training_loop.sh"
    c, o, e = run(
        ["bash", str(script), "--dry-run", "--dry-run"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("double dry-run", c == 0 and "DEPLOYMENT_COMPLETE" not in o)

    # extra positional after a flag is unknown option
    c, o, e = run(
        ["bash", str(script), "--dry-run", "extra"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("extra positional rejected", c != 0 and "Unknown option" in e)

    # canonical present and matches schema
    rec("canonical exists", CANON.is_file())
    canon = json.loads(CANON.read_text())
    rec("canonical status", canon.get("status") == "DEPLOYMENT_COMPLETE")
    rec("canonical schema", canon.get("schema") == "kg.production-training-result/v2")

    # activator size bounds and shebang prefix exact
    for slug in COMPONENTS:
        p = BIN / f"activate_{slug}.sh"
        size = p.stat().st_size if p.is_file() else 0
        rec(f"{slug} size bounds", 200 < size < 20000, f"size={size}")
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        rec(f"{slug} shebang bash", text.startswith("#!/usr/bin/env bash") or text.startswith("#!/bin/bash"))

    # bridge type and key contracts
    c, o, e = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    data = json.loads(o)
    rec("bridge no nan", "NaN" not in o and "Infinity" not in o)
    c704 = data.get("control704", {})
    rec("control704 keys", set(c704) == {"access", "priority", "identity", "lattice"})
    rec("access has VIP", "VIP" in c704.get("access", "") and "proxy" in c704.get("access", ""))
    integ = data.get("integration", {})
    rec("integration non-empty", all(isinstance(v, str) and v for v in integ.values()))
    rec("next starts export", data.get("next_operator_action", "").startswith("export ALLOW_AGENT_EXECUTION=true"))
    agents = data.get("agents", {}).get("agents", [])
    roles = [a.get("role") for a in agents]
    rec("roles unique", len(set(roles)) == len(roles) == 6)
    rec("gate_satisfied bool", data.get("agents", {}).get("gate_satisfied") is True)
    rec("agents status registered", data.get("agents", {}).get("status") == "AGENTS_REGISTERED")

    # small concurrent burst: 8 status + 8 bridge
    results = []
    lock = threading.Lock()

    def hit(kind):
        if kind == "status":
            code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
            ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
        else:
            code, out, err = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
            ok = code == 0 and """""AGENTS_REGISTERED"""" in out
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=hit, args=(k,)) for k in ["status", "bridge"] * 8]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    rec("16-way concurrent", len(results) == 16 and all(results), f"got={len(results)}")

    # refuse does not write into runs/
    runs = ROOT / "runs"
    before = set(runs.iterdir()) if runs.is_dir() else set()
    run(["bash", str(MASTER), "status"], clear=True)
    after = set(runs.iterdir()) if runs.is_dir() else set()
    rec("refuse no runs write", before == after)

    print(f"Results: {PASS} passed · {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
