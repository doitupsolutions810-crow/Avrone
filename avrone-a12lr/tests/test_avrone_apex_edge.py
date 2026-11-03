#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Apex-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Nineteenth-tier suite. Prior cumulative: 1207 passed. New apex/chaos layers:
  - Gate with SQL/NoSQL injection-like strings around true
  - Activator label never contains the substring 'refused'
  - Bridge integration keys all non-empty strings
  - Parallel 50× dry-run only on rotating components
  - --input with JSON object of 200 keys
  - Component names with mixed case of real component slugs
  - Verify gate_satisfied is never the string 'true' or 'false'
  - Dashboard contains 'Avrone' or 'Due'
  - Master status with trailing garbage env vars
  - Stress: 100× sequential master 'all'
  - Cross-check agent id list sorted equals expected sorted set
  - Refuse stderr or stdout is non-empty
  - Confirm every activate_*.sh starts with #!/usr/bin/env bash or #!/bin/bash or #!
  - Activator JSON does not contain the key 'error' on success
  - Rapid 20× status + 20× all + 20× unknown parallel
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

EXPECTED_AGENT_IDS = sorted([
    "grok-bot-supervisor",
    "grok-bot-computer-use",
    "grok-bot-github-bridge",
    "avrone-fastapi-control",
    "avrone-react-dashboard",
    "persistent-memory-agent",
])


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


# ─── Injection-like true ─────────────────────────────────────────────────────

def test_injection_like_true() -> None:
    print("\n── SQL/NoSQL Injection-Like true Gates ──")
    samples = [
        "true' OR '1'='1",
        'true" OR "1"="1',
        "true; DROP TABLE x;--",
        "true{$ne:null}",
        "true && true",
        "true || false",
        "${true}",
        "$(true)",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"inject [{val[:30]!r}…] refuses", code != 0, f"code={code}")


# ─── label never refused ─────────────────────────────────────────────────────

def test_label_no_refused() -> None:
    print("\n── Activator label Never Contains 'refused' ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        label = str(obj.get("label", "")).lower()
        record(f"{c} label no 'refused'", "refused" not in label)


# ─── integration keys non-empty ──────────────────────────────────────────────

def test_integration_nonempty() -> None:
    print("\n── Bridge integration Keys Non-Empty ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    integ = data.get("integration", {})
    for k in ("grok_bot_repository", "mac_autopilot_zip_ref", "cursor_agents"):
        val = integ.get(k)
        record(f"integration.{k} non-empty str", isinstance(val, str) and len(val) > 0, f"val={val!r}")


# ─── Parallel 50 dry-run rotating ────────────────────────────────────────────

def test_parallel_50_dryrun() -> None:
    print("\n── Parallel 50× Rotating Dry-Run ──")
    def one(i: int) -> bool:
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(50)]
        for f in as_completed(futs):
            results.append(f.result())
    record("50/50 parallel dry-run", all(results), f"ok={sum(results)}")


# ─── 200-key object --input ──────────────────────────────────────────────────

def test_200_key_input() -> None:
    print("\n── 200-Key Object --input ──")
    script = BIN / "activate_sqlite_persistence.sh"
    payload = {f"k{i}": i for i in range(200)}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("200-key --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Mixed-case real slugs ───────────────────────────────────────────────────

def test_mixed_case_slugs() -> None:
    print("\n── Mixed-Case Real Component Slugs ──")
    for c in COMPONENTS[:6]:
        mixed = c.swapcase()
        code, out, err = run_cmd(
            ["bash", str(MASTER), mixed],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # wrong case → unknown / non-zero
        record(f"mixed-case [{mixed}] not clean success", not (code == 0 and "components_registered" in out), f"code={code}")


# ─── gate_satisfied not string ───────────────────────────────────────────────

def test_gate_satisfied_not_str() -> None:
    print("\n── gate_satisfied Not String 'true'/'false' ──")
    for c in ["agent_registry_orchestrator", "persistent_memory_agent"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        gs = obj.get("gate_satisfied")
        record(f"{c} gate_satisfied is bool", isinstance(gs, bool))
        record(f"{c} gate_satisfied is not str", not isinstance(gs, str))


# ─── Dashboard Avrone/Due ────────────────────────────────────────────────────

def test_dashboard_avrone() -> None:
    print("\n── Dashboard Avrone / Due ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has Avrone or Due", "Avrone" in text or "Due" in text)


# ─── 100× master all ─────────────────────────────────────────────────────────

def test_100_master_all() -> None:
    print("\n── 100× Sequential Master 'all' ──")
    ok = 0
    for _ in range(100):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "all"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("100/100 master all", ok == 100, f"ok={ok}")


# ─── Agent id set exact ──────────────────────────────────────────────────────

def test_agent_id_set() -> None:
    print("\n── Agent ID Set Exact Match ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    ids = sorted(a["id"] for a in data["agents"]["agents"])
    record("agent ids exact expected set", ids == EXPECTED_AGENT_IDS, f"ids={ids}")


# ─── Refuse non-empty output ─────────────────────────────────────────────────

def test_refuse_nonempty() -> None:
    print("\n── Refuse Produces Non-Empty Output ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = (out + err).strip()
    record("refuse output non-empty", len(combined) > 0, f"len={len(combined)}")


# ─── Shebang starts with #! ──────────────────────────────────────────────────

def test_shebang_hashbang() -> None:
    print("\n── All Activators Start With #! ──")
    for c in COMPONENTS:
        head = (BIN / f"activate_{c}.sh").read_text(encoding="utf-8", errors="replace")[:20]
        record(f"{c} starts with #!", head.startswith("#!"))
    head = MASTER.read_text(encoding="utf-8", errors="replace")[:20]
    record("master starts with #!", head.startswith("#!"))


# ─── No error key on success ─────────────────────────────────────────────────

def test_no_error_key() -> None:
    print("\n── Success JSON Has No 'error' Key ──")
    for c in ["fastapi_control_plane", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} no 'error' key", "error" not in obj)


# ─── status + all + unknown parallel ─────────────────────────────────────────

def test_status_all_unknown_parallel() -> None:
    print("\n── 20× status + 20× all + 20× unknown Parallel ──")
    def status(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def all_(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "all"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def unknown(i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), f"apex_unknown_{i}"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code != 0

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = (
            [pool.submit(status, i) for i in range(20)]
            + [pool.submit(all_, i) for i in range(20)]
            + [pool.submit(unknown, i) for i in range(20)]
        )
        for f in as_completed(futs):
            results.append(f.result())
    record(f"60/60 status/all/unknown", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Apex-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_injection_like_true()
    test_label_no_refused()
    test_integration_nonempty()
    test_parallel_50_dryrun()
    test_200_key_input()
    test_mixed_case_slugs()
    test_gate_satisfied_not_str()
    test_dashboard_avrone()
    test_100_master_all()
    test_agent_id_set()
    test_refuse_nonempty()
    test_shebang_hashbang()
    test_no_error_key()
    test_status_all_unknown_parallel()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
