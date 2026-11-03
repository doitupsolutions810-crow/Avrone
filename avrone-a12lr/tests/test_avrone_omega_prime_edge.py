#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Omega-Prime Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Seventeenth-tier suite. Prior cumulative: 1085 passed. New omega-prime layers:
  - Gate with URL-encoded / percent-encoded true (%74%72%75%65)
  - Activator status is always a non-empty string under gate
  - Bridge priority is exactly the string 'high' (case-sensitive)
  - Parallel 25× each of status, refuse, dry, live (100 total)
  - --input with JSON null values and boolean values mixed
  - Component names containing only quotes or backticks
  - Verify dependencies_resolved type is int not float
  - Dashboard contains 'DEPLOYMENT_COMPLETE' badge text
  - Master with env ALLOW_AGENT_EXECUTION=true and empty extra args
  - Stress: 400× sequential status under gate
  - Cross-check all agent statuses are 'registered' or 'implementation_ready'
  - Refuse message contains 'ALLOW_AGENT_EXECUTION'
  - Confirm bin/ has exactly 13 activate_*.sh files (12 + master)
  - Activator label contains at least one alphabetic character
  - Rapid alternate 60× dry-run / live on same component
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


# ─── Percent-encoded true ────────────────────────────────────────────────────

def test_percent_encoded_true() -> None:
    print("\n── Percent-Encoded true Gate Values ──")
    samples = [
        "%74%72%75%65",           # true
        "%74rue",
        "t%72ue",
        "tr%75e",
        "tru%65",
        "%54%52%55%45",           # TRUE
        "true%00",
        "true%20",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"pct [{val}] refuses", code != 0, f"code={code}")


# ─── status non-empty string ─────────────────────────────────────────────────

def test_status_nonempty_str() -> None:
    print("\n── Activator status Non-Empty String ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        st = obj.get("status")
        record(f"{c} status is non-empty str", isinstance(st, str) and len(st) > 0, f"status={st!r}")


# ─── priority exact high ─────────────────────────────────────────────────────

def test_priority_case() -> None:
    print("\n── control704.priority Exact 'high' Case-Sensitive ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    p = data.get("control704", {}).get("priority")
    record("priority is exactly 'high'", p == "high", f"priority={p!r}")


# ─── Parallel 100 mixed kinds ────────────────────────────────────────────────

def test_parallel_100_kinds() -> None:
    print("\n── Parallel 25× status + refuse + dry + live ──")
    def status(_i: int) -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        return c == 0

    def refuse(_i: int) -> bool:
        c, o, e = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return c != 0

    def dry(i: int) -> bool:
        comp = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{comp}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def live(i: int) -> bool:
        comp = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{comp}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = (
            [pool.submit(status, i) for i in range(25)]
            + [pool.submit(refuse, i) for i in range(25)]
            + [pool.submit(dry, i) for i in range(25)]
            + [pool.submit(live, i) for i in range(25)]
        )
        for f in as_completed(futs):
            results.append(f.result())
    record(f"100/100 parallel kinds", all(results), f"ok={sum(results)}")


# ─── null/bool --input ───────────────────────────────────────────────────────

def test_null_bool_input() -> None:
    print("\n── --input With null and bool Values ──")
    script = BIN / "activate_persistent_memory_agent.sh"
    payload = {"a": None, "b": True, "c": False, "d": 0, "e": "", "f": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("null/bool --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Quote/backtick components ───────────────────────────────────────────────

def test_quote_components() -> None:
    print("\n── Quote / Backtick Component Names ──")
    for slug in ["'", '"', "''", '""', "`", "``", "'\"", "\"'"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"quote [{slug!r}] no hang", code != -1, f"code={code}")


# ─── deps type int ───────────────────────────────────────────────────────────

def test_deps_type_int() -> None:
    print("\n── dependencies_resolved Type int ──")
    for c in ["fastapi_control_plane", "langgraph_supervisor", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        dr = obj.get("integrity", {}).get("dependencies_resolved")
        record(f"{c} deps is int", isinstance(dr, int) and not isinstance(dr, bool), f"type={type(dr).__name__}")
        record(f"{c} deps == 100", dr == 100)


# ─── Dashboard badge ─────────────────────────────────────────────────────────

def test_dashboard_badge_text() -> None:
    print("\n── Dashboard DEPLOYMENT_COMPLETE Badge ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("badge DEPLOYMENT_COMPLETE present", "DEPLOYMENT_COMPLETE" in text)


# ─── 400× status ─────────────────────────────────────────────────────────────

def test_400_status() -> None:
    print("\n── 400× Sequential Status ──")
    ok = 0
    for _ in range(400):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok += 1
    record("400/400 status", ok == 400, f"ok={ok}")


# ─── Agent status values ─────────────────────────────────────────────────────

def test_agent_status_values() -> None:
    print("\n── Agent status Values Allowed Set ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    allowed = {"registered", "implementation_ready"}
    for a in data["agents"]["agents"]:
        st = a.get("status")
        record(f"{a['id']} status in allowed set", st in allowed, f"status={st!r}")


# ─── Refuse contains ALLOW ───────────────────────────────────────────────────

def test_refuse_contains_allow() -> None:
    print("\n── Refuse Contains ALLOW_AGENT_EXECUTION ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = out + err
    record("refuse mentions ALLOW_AGENT_EXECUTION", "ALLOW_AGENT_EXECUTION" in combined)


# ─── bin activate count ──────────────────────────────────────────────────────

def test_bin_activate_count() -> None:
    print("\n── bin/ activate_*.sh Count ──")
    files = list(BIN.glob("activate_*.sh"))
    record("exactly 13 activate_*.sh (12 + master)", len(files) == 13, f"count={len(files)}")
    names = {f.name for f in files}
    record("master present", "activate_avrone.sh" in names)


# ─── label has alpha ─────────────────────────────────────────────────────────

def test_label_has_alpha() -> None:
    print("\n── Activator label Has Alphabetic Char ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        label = obj.get("label", "")
        record(f"{c} label has alpha", any(ch.isalpha() for ch in label), f"label={label!r}")


# ─── 60× alternate dry/live ──────────────────────────────────────────────────

def test_60_alt_dry_live() -> None:
    print("\n── 60× Alternate Dry-Run / Live (same component) ──")
    script = BIN / "activate_control12_lattice_op.sh"
    ok = 0
    for i in range(60):
        if i % 2 == 0:
            code, out, err = run_cmd(
                ["bash", str(script), "--dry-run"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0:
                ok += 1
        else:
            code, out, err = run_cmd(
                ["bash", str(script)],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
    record("60/60 alt dry/live", ok == 60, f"ok={ok}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Omega-Prime Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_percent_encoded_true()
    test_status_nonempty_str()
    test_priority_case()
    test_parallel_100_kinds()
    test_null_bool_input()
    test_quote_components()
    test_deps_type_int()
    test_dashboard_badge_text()
    test_400_status()
    test_agent_status_values()
    test_refuse_contains_allow()
    test_bin_activate_count()
    test_label_has_alpha()
    test_60_alt_dry_live()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
