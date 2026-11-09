#!/usr/bin/env python3
"""AVRONE-12 — Trench-level edge suite.

Covers cases the named suites do not: byte-identity of status/all,
stderr-only refusals, dry-run request shape, --output isolation,
CWD independence, COMPONENT env isolation, and lattice scoping.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
MASTER = BIN / "activate_avrone.sh"
CANON = ROOT / "references" / "canonical-deployment.json"

PASS = FAIL = 0
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


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(cmd, env=None, clear=False, timeout=8.0, cwd=None):
    e = os.environ.copy()
    if clear:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd or ROOT), env=e,
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def main() -> int:
    print("AVRONE-12 Trench-level edge suite")

    # status and all are the same payload
    c1, o1, e1 = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
    c2, o2, e2 = run(["bash", str(MASTER), "all"], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("status exit 0", c1 == 0)
    rec("all exit 0", c2 == 0)
    rec("status == all bytes", o1 == o2 and o1.strip() != "")
    rec("status stderr empty", e1.strip() == "")
    rec("all stderr empty", e2.strip() == "")
    obj = json.loads(o1)
    rec("components_registered 12", obj.get("components_registered") == 12)
    rec("status has no error key", "error" not in obj)
    rec("schema exact", obj.get("schema") == "kg.production-training-result/v2")

    # refusal is stderr-only and does not leak success
    c, o, e = run(["bash", str(MASTER), "status"], clear=True)
    rec("unset exit nonzero", c != 0)
    rec("unset stdout empty", o.strip() == "")
    rec("unset reason phrase", "ALLOW_AGENT_EXECUTION is not set to true" in e)
    rec("unset no COMPLETE", "DEPLOYMENT_COMPLETE" not in o and "DEPLOYMENT_COMPLETE" not in e)

    for bad in ["trueX", "xtrue", "true\n", " true", "true ", "TRUE", "True", "1", "yes", "on"]:
        c, o, e = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": bad})
        rec(f"gate {bad!r} refuses", c != 0 and o.strip() == "")

    # unknown / odd components are not status
    for slug in ["--", " ", "status ", " status", "../status", "status/extra", "true", "CON"]:
        c, o, e = run(["bash", str(MASTER), slug], env={"ALLOW_AGENT_EXECUTION": "true"})
        rec(f"slug {slug!r} not status", c != 0 and "components_registered" not in o, f"code={c}")

    c, o, e = run(["bash", str(MASTER), "no_such_component_xyz"], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("unknown echoes name", "no_such_component_xyz" in e and c != 0)
    rec("unknown stdout empty", o.strip() == "")

    # COMPONENT env must not override argv
    c, o, e = run(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true", "COMPONENT": "skill_factory"},
    )
    rec("COMPONENT env ignored", c == 0 and "components_registered" in o)

    # CWD independence: ROOT is derived from BASH_SOURCE
    with tempfile.TemporaryDirectory() as td:
        c, o, e = run(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            cwd=td,
        )
        rec("status from foreign CWD", c == 0 and "DEPLOYMENT_COMPLETE" in o, f"code={c}")

    # locale noise does not change the payload
    c, o, e = run(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true", "LC_ALL": "C", "LANG": "C"},
    )
    rec("LC_ALL=C payload match", o == o1)

    # double invoke stability
    c3, o3, _ = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("double status stable", o3 == o1 and c3 == 0)

    # dry-run returns the request, not the result
    script = BIN / "activate_skill_factory.sh"
    c, o, e = run(["bash", str(script), "--dry-run"], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("dry-run exit 0", c == 0)
    rec("dry-run not COMPLETE", "DEPLOYMENT_COMPLETE" not in o)
    try:
        dry = json.loads(o)
        rec("dry-run has component", dry.get("component") == "skill_factory")
        rec("dry-run has gate", "ALLOW_AGENT_EXECUTION=true" in str(dry.get("gate", "")))
    except Exception as exc:
        rec("dry-run json", False, str(exc))

    # --input replaces the request on dry-run
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"probe": 1, "component": "skill_factory"}, f)
        inp = f.name
    try:
        c, o, e = run(
            ["bash", str(script), "--dry-run", "--input", inp],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        rec("dry-run input echoed", c == 0 and '"probe": 1' in o.replace(" ", ""))
    finally:
        os.unlink(inp)

    # --output writes the result and leaves stdout empty
    outp = ROOT / "runs" / "trench-out.json"
    outp.parent.mkdir(exist_ok=True)
    c, o, e = run(
        ["bash", str(script), "--output", str(outp)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("output file written", outp.exists() and "DEPLOYMENT_COMPLETE" in outp.read_text())
    rec("output stdout empty", o.strip() == "")
    outp.unlink(missing_ok=True)

    # unknown option is a clean non-zero
    c, o, e = run(
        ["bash", str(script), "--not-a-flag"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("unknown option nonzero", c != 0 and "Unknown option" in e)

    # missing --input value does not hang
    c, o, e = run(
        ["bash", str(script), "--input"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        timeout=4.0,
    )
    rec("missing --input no hang", c != -1, f"code={c}")

    # every activator: shebang, component field, integrity, files_created
    for slug in COMPONENTS:
        path = BIN / f"activate_{slug}.sh"
        rec(f"{slug} exists", path.is_file())
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        rec(f"{slug} shebang", text.startswith("#!"))
        c, o, e = run(["bash", str(path)], env={"ALLOW_AGENT_EXECUTION": "true"})
        try:
            body = json.loads(o)
        except Exception:
            body = {}
        rec(f"{slug} complete", c == 0 and body.get("status") == "DEPLOYMENT_COMPLETE")
        rec(f"{slug} field == slug", body.get("component") == slug)
        rec(f"{slug} label != slug", body.get("label") not in (None, "", slug))
        rec(f"{slug} gate bool", body.get("gate_satisfied") is True)
        integ = body.get("integrity") or {}
        rec(f"{slug} deps 100", integ.get("dependencies_resolved") == 100)
        rec(f"{slug} files 142", integ.get("files_created") == 142)
        rec(f"{slug} compile ok", integ.get("python_compile_ok") is True)
        rec(f"{slug} source canonical", "canonical" in str(body.get("source", "")))
        rec(f"{slug} success stderr empty", e.strip() == "")

    # bridge contract
    c, o, e = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    data = json.loads(o)
    rec("bridge complete", data.get("status") == "DEPLOYMENT_COMPLETE")
    rec("bridge identity", data.get("control704", {}).get("identity") == "Control704 IT Technician")
    rec("bridge priority", data.get("control704", {}).get("priority") == "high")
    rec("next action", "activate_avrone.sh" in data.get("next_operator_action", ""))
    rec("cursor bridged", data.get("integration", {}).get("cursor_agents") == "bridged")
    rec("zip ref fragment", "e05e21fe" in data.get("integration", {}).get("mac_autopilot_zip_ref", ""))
    agents = data.get("agents", {}).get("agents", [])
    ids = [a.get("id") for a in agents]
    rec("agent count 6", data.get("agents", {}).get("count") == 6 and len(ids) == 6)
    rec("agent ids unique", len(set(ids)) == len(ids))
    rec(
        "lattice only on grok-bot",
        all((a.get("lattice") == "Control12") == a.get("id", "").startswith("grok-bot-") for a in agents),
    )
    ports = {a["id"]: a.get("port") for a in agents if "port" in a}
    rec("fastapi port int", ports.get("avrone-fastapi-control") == 8000 and isinstance(ports.get("avrone-fastapi-control"), int))
    rec("dashboard port int", ports.get("avrone-react-dashboard") == 3000 and isinstance(ports.get("avrone-react-dashboard"), int))
    rec("schema match canonical", data.get("schema") == json.loads(CANON.read_text())["schema"])

    c, o, e = run([sys.executable, str(BRIDGE)], clear=True)
    pending = json.loads(o)
    rec("ungated GATE_PENDING", pending.get("status") == "GATE_PENDING")
    rec("ungated agents false", pending.get("agents", {}).get("gate_satisfied") is False)

    print(f"Results: {PASS} passed · {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
