#!/usr/bin/env python3
"""AVRONE-12 — Rift-level edge suite. New classes beyond the void suite."""
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
AGENTS = ROOT / "agents" / "grok_bot_integration.py"
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
    except ValueError as exc:
        return -2, "", f"ValueError:{exc}"


def main() -> int:
    print("AVRONE-12 Rift-level edge suite")

    for val in ["ｔｒｕｅ", "truе", "true\n", "true;true", "yes", "1", "on", "${true}"]:
        code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": val})
        rec(f"lookalike [{val!r}] refuses", code not in (0, -1), f"code={code}")

    code, out, err = run(["bash", str(MASTER)], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("no-args defaults to status", code == 0 and "components_registered" in out)

    code, out, err = run(["bash", str(MASTER), "all"], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("all matches status schema", code == 0 and json.loads(out).get("components_registered") == 12)

    name = "rift_unknown_77"
    code, out, err = run(["bash", str(MASTER), name], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("unknown embeds name", code != 0 and name in (out + err))

    code, out, err = run(["bash", str(MASTER), "status"], clear=True)
    rec("refuse hides COMPLETE", code != 0 and "DEPLOYMENT_COMPLETE" not in out)

    labels = []
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        head = path.read_bytes()[:2]
        mode = path.stat().st_mode
        rec(f"{c} shebang", head == b"#!")
        rec(f"{c} not world-writable", not (mode & stat.S_IWOTH))
        code, out, err = run(["bash", str(path)], env={"ALLOW_AGENT_EXECUTION": "true"})
        obj = json.loads(out)
        labels.append(obj["label"])
        rec(f"{c} label != slug", obj["label"] != obj["component"])
        rec(f"{c} files_created 142", obj["integrity"]["files_created"] == 142)
        rec(f"{c} no error key", "error" not in obj)
        code, out, err = run(["bash", str(path), "--dry-run"], env={"ALLOW_AGENT_EXECUTION": "true"})
        dry = json.loads(out)
        rec(f"{c} dry-run object", isinstance(dry, dict) and dry.get("component") == c)

    rec("labels unique", len(labels) == len(set(labels)))

    def live(c):
        code, out, err = run(["bash", str(BIN / f"activate_{c}.sh")], env={"ALLOW_AGENT_EXECUTION": "true"})
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def refuse(_):
        code, out, err = run(["bash", str(MASTER), "status"], clear=True)
        return code != 0 and "DEPLOYMENT_COMPLETE" not in out

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(live, c) for c in COMPONENTS] + [pool.submit(refuse, i) for i in range(6)]
        for f in as_completed(futs):
            results.append(f.result())
    rec(f"{sum(results)}/{len(results)} live+refuse parallel", all(results))

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump({"nested": {"a": [None, True, "x"]}}, tf)
        path = tf.name
    try:
        code, out, err = run(
            ["bash", str(BIN / "activate_sqlite_persistence.sh"), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        rec("nested --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)

    code, out, err = run(
        ["bash", str(BIN / "activate_react_dashboard.sh"), "--input", str(ROOT / "bin")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    rec("directory --input no hang", code != -1, f"code={code}")

    code, out, err = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    bridge = json.loads(out)
    code2, out2, err2 = run([sys.executable, str(AGENTS)], env={"ALLOW_AGENT_EXECUTION": "true"})
    agents = json.loads(out2)
    ids_b = sorted(a["id"] for a in bridge["agents"]["agents"])
    ids_a = sorted(a["id"] for a in agents["agents"]["agents"])
    rec("bridge/agents ids match", ids_b == ids_a and len(ids_b) == 6)
    rec("identity exact", bridge["control704"]["identity"] == "Control704 IT Technician")
    rec("cursor bridged", bridge["integration"]["cursor_agents"] == "bridged")
    canon = json.loads(CANON.read_text())
    rec("canonical version present", bool(canon.get("build_context", {}).get("version")))

    html = DASH.read_text(encoding="utf-8")
    rec("table balanced", html.lower().count("<table") == html.lower().count("</table>"))
    rec("six registered copy", "6 registered" in html)

    print(f"Results: {PASS} passed · {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
