#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Quantum-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Tenth-tier suite. Prior cumulative: 646 passed. New quantum/chaos layers:
  - Gate values that are only whitespace + true permutations
  - Activator source field exact string match
  - Bridge schema exact match to canonical
  - Parallel 50× master status under gate
  - Parallel 50× refuse under no-gate
  - --input with JSON array of 1000 small objects
  - Component names that look like options but are positional
  - Verify label contains human-readable words for each activator
  - Dashboard CSS variable presence (design tokens)
  - Repeated canonical file read consistency (hash)
  - Master when COMPONENT env is set does not override argv
  - Stress: 15× full 12-component sequential sweep
  - Bridge agents status string exact AGENTS_REGISTERED
  - gate_satisfied bool strict True under gate
  - Empty ROOT-relative --input path
  - Confirm no .pyc pollution from agent modules after runs
"""
from __future__ import annotations

import hashlib
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

SOURCE_EXACT = "canonical kg.production-training-result/v2"


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


# ─── Whitespace + true permutations ──────────────────────────────────────────

def test_ws_true_perms() -> None:
    print("\n── Whitespace + true Permutations ──")
    samples = [
        " true",
        "true ",
        " true ",
        "\ttrue",
        "true\t",
        "\ntrue",
        "true\n",
        "true\r",
        "true\r\n",
        "t rue",
        "t\true",
        "TRUE",
        "True",
        "tRuE",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"ws/case [{val!r}] refuses", code != 0, f"code={code}")


# ─── source field exact ──────────────────────────────────────────────────────

def test_source_exact() -> None:
    print("\n── Activator source Field Exact ──")
    for c in ["fastapi_control_plane", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} source exact", obj.get("source") == SOURCE_EXACT, f"source={obj.get('source')!r}")


# ─── Bridge schema == canonical ──────────────────────────────────────────────

def test_bridge_schema_match() -> None:
    print("\n── Bridge Schema == Canonical ──")
    canon = json.loads(CANONICAL.read_text())
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    bridge = json.loads(out)
    record("bridge.schema == canonical.schema", bridge.get("schema") == canon.get("schema"))


# ─── Parallel 50 status / 50 refuse ──────────────────────────────────────────

def test_parallel_50_status() -> None:
    print("\n── Parallel 50× Master Status ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(50)]
        for f in as_completed(futs):
            results.append(f.result())
    record("50/50 parallel status", all(results), f"ok={sum(results)}")


def test_parallel_50_refuse() -> None:
    print("\n── Parallel 50× Refuse ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return code != 0

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(50)]
        for f in as_completed(futs):
            results.append(f.result())
    record("50/50 parallel refuse", all(results), f"ok={sum(results)}")


# ─── 1000-object array --input ───────────────────────────────────────────────

def test_1000_object_input() -> None:
    print("\n── 1000-Object Array --input ──")
    script = BIN / "activate_sqlite_persistence.sh"
    payload = [{"i": i, "v": f"val{i}"} for i in range(1000)]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=15,
        )
        record("1000-object --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Option-like positional ──────────────────────────────────────────────────

def test_option_like_positional() -> None:
    print("\n── Option-Like Positional Components ──")
    for slug in ["--dry-run", "--input", "--output", "-n", "--"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"positional [{slug}] no hang", code != -1, f"code={code}")


# ─── Label human-readable ────────────────────────────────────────────────────

def test_label_human() -> None:
    print("\n── Activator Labels Human-Readable ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        label = obj.get("label", "")
        # at least one space or capital letter indicating words
        ok = isinstance(label, str) and len(label) > 3 and any(ch.isupper() or ch.isspace() for ch in label)
        record(f"{c} label human-readable", ok, f"label={label!r}")


# ─── Dashboard CSS vars ──────────────────────────────────────────────────────

def test_dashboard_css_vars() -> None:
    print("\n── Dashboard CSS Design Tokens ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has --bg or --panel token", "--bg" in text or "--panel" in text)
    record("has --green or --accent", "--green" in text or "--accent" in text)


# ─── Canonical hash consistency ──────────────────────────────────────────────

def test_canonical_hash() -> None:
    print("\n── Canonical File Hash Consistency ──")
    h1 = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    h2 = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    h3 = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    record("canonical hash stable across 3 reads", h1 == h2 == h3, f"hash={h1[:16]}…")


# ─── COMPONENT env does not override argv ────────────────────────────────────

def test_component_env_no_override() -> None:
    print("\n── COMPONENT Env Does Not Override argv ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true", "COMPONENT": "evil_override"},
    )
    record("status still succeeds with COMPONENT env", code == 0 and "DEPLOYMENT_COMPLETE" in out)
    # should not say unknown evil
    record("no evil_override leak", "evil_override" not in out and "evil_override" not in err)


# ─── 15× full sweep ──────────────────────────────────────────────────────────

def test_15_full_sweeps() -> None:
    print("\n── 15× Full 12-Component Sequential Sweep ──")
    ok_sweeps = 0
    for _ in range(15):
        good = True
        for c in COMPONENTS:
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if not (code == 0 and "DEPLOYMENT_COMPLETE" in out):
                good = False
                break
        if good:
            ok_sweeps += 1
    record("15/15 full sweeps", ok_sweeps == 15, f"ok={ok_sweeps}")


# ─── AGENTS_REGISTERED exact ─────────────────────────────────────────────────

def test_agents_registered_exact() -> None:
    print("\n── agents.status Exact AGENTS_REGISTERED ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    st = data.get("agents", {}).get("status")
    record("status == AGENTS_REGISTERED", st == "AGENTS_REGISTERED", f"status={st!r}")


# ─── gate_satisfied strict True ──────────────────────────────────────────────

def test_gate_satisfied_true() -> None:
    print("\n── gate_satisfied Strict True Under Gate ──")
    for c in ["agent_registry_orchestrator", "persistent_memory_agent"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} gate_satisfied is True", obj.get("gate_satisfied") is True)


# ─── Empty relative --input ──────────────────────────────────────────────────

def test_empty_relative_input() -> None:
    print("\n── Empty / Relative Odd --input Paths ──")
    script = BIN / "activate_secure_cli_execution.sh"
    for path in ["", ".", "./"]:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=8,
        )
        record(f"--input {path!r} no hang", code != -1, f"code={code}")


# ─── No .pyc pollution check ─────────────────────────────────────────────────

def test_no_pyc_pollution() -> None:
    print("\n── No .pyc Pollution in agents/ ──")
    # run agents module then check
    run_cmd([sys.executable, str(AGENTS)], env={"ALLOW_AGENT_EXECUTION": "true"})
    pycs = list((ROOT / "agents").rglob("*.pyc"))
    pycs += list((ROOT / "agents").rglob("__pycache__"))
    # __pycache__ may appear; we only flag unexpected top-level .pyc next to sources
    top_pyc = list((ROOT / "agents").glob("*.pyc"))
    record("no top-level .pyc beside sources", len(top_pyc) == 0, f"found={top_pyc}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Quantum-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_ws_true_perms()
    test_source_exact()
    test_bridge_schema_match()
    test_parallel_50_status()
    test_parallel_50_refuse()
    test_1000_object_input()
    test_option_like_positional()
    test_label_human()
    test_dashboard_css_vars()
    test_canonical_hash()
    test_component_env_no_override()
    test_15_full_sweeps()
    test_agents_registered_exact()
    test_gate_satisfied_true()
    test_empty_relative_input()
    test_no_pyc_pollution()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
