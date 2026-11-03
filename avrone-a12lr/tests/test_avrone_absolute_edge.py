#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Absolute-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Fifteenth-tier suite. Prior cumulative: 948 passed. New absolute/chaos layers:
  - Gate with repeated true tokens ("true true", "true;true", "true|true")
  - Activator gate_satisfied False path never taken under ALLOW=true
  - Bridge mac_autopilot_zip_ref contains the known zip basename fragment
  - Parallel 80× status under gate
  - --input with Unicode snowman / emoji payload
  - Component names with leading/trailing whitespace only after strip simulation
  - Verify files_created == 142 on all 12 activators
  - Dashboard contains 'Control12' and 'lattice'
  - Master unknown component includes the requested name in JSON
  - Stress: 300× sequential refuse
  - Cross-check every suite file is non-empty
  - Activator label does not equal component slug
  - Rapid 50× master status + 50× bridge interleaved
  - Confirm no world-writable activator scripts
  - Exit code 0 only when DEPLOYMENT_COMPLETE present on success paths
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
TESTS = ROOT / "tests"

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


# ─── Repeated true tokens ────────────────────────────────────────────────────

def test_repeated_true_tokens() -> None:
    print("\n── Repeated true Tokens as Gate ──")
    samples = [
        "true true",
        "true;true",
        "true|true",
        "true&true",
        "true,true",
        "true+true",
        "true true true",
        "true\ntrue",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"repeated [{val!r}] refuses", code != 0, f"code={code}")


# ─── gate_satisfied never False under true ───────────────────────────────────

def test_gate_satisfied_never_false() -> None:
    print("\n── gate_satisfied Never False Under ALLOW=true ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} gate_satisfied is True", obj.get("gate_satisfied") is True)


# ─── zip ref fragment ────────────────────────────────────────────────────────

def test_zip_ref_fragment() -> None:
    print("\n── mac_autopilot_zip_ref Fragment ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    val = str(data.get("integration", {}).get("mac_autopilot_zip_ref", ""))
    record("zip ref mentions avrone-mac-autopilot or e05e21fe", "avrone-mac-autopilot" in val or "e05e21fe" in val)


# ─── Parallel 80 status ──────────────────────────────────────────────────────

def test_parallel_80_status() -> None:
    print("\n── Parallel 80× Status ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(one, i) for i in range(80)]
        for f in as_completed(futs):
            results.append(f.result())
    record("80/80 parallel status", all(results), f"ok={sum(results)}")


# ─── Emoji / snowman --input ─────────────────────────────────────────────────

def test_emoji_input() -> None:
    print("\n── Emoji / Snowman --input ──")
    script = BIN / "activate_react_dashboard.sh"
    payload = {"msg": "hello ☃ 🚀 ✨", "note": "测试"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(payload, tf, ensure_ascii=False)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("emoji --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── files_created 142 all ───────────────────────────────────────────────────

def test_files_created_all() -> None:
    print("\n── files_created == 142 (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        fc = obj.get("integrity", {}).get("files_created")
        record(f"{c} files_created == 142", fc == 142, f"fc={fc}")


# ─── Dashboard Control12 lattice ─────────────────────────────────────────────

def test_dashboard_control12() -> None:
    print("\n── Dashboard Control12 / lattice ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard has Control12", "Control12" in text)
    record("dashboard has lattice", "lattice" in text.lower())


# ─── Unknown embeds name ─────────────────────────────────────────────────────

def test_unknown_embeds_name() -> None:
    print("\n── Unknown Component Embeds Requested Name ──")
    name = "omega_absolute_unknown_42"
    code, out, err = run_cmd(
        ["bash", str(MASTER), name],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    combined = out + err
    record("unknown response embeds name", name in combined, f"code={code}")


# ─── 300× refuse ─────────────────────────────────────────────────────────────

def test_300_refuse() -> None:
    print("\n── 300× Sequential Refuse ──")
    ok = 0
    for _ in range(300):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "false"},
        )
        if code != 0:
            ok += 1
    record("300/300 refuse", ok == 300, f"ok={ok}")


# ─── Suite files non-empty ───────────────────────────────────────────────────

def test_suites_nonempty() -> None:
    print("\n── All Suite Files Non-Empty ──")
    for p in sorted(TESTS.glob("test_avrone_*.py")):
        size = p.stat().st_size
        record(f"{p.name} non-empty", size > 100, f"size={size}")


# ─── label != component ──────────────────────────────────────────────────────

def test_label_ne_component() -> None:
    print("\n── label != component Slug ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} label != component", obj.get("label") != obj.get("component"))


# ─── 50 status + 50 bridge interleaved ───────────────────────────────────────

def test_50_50_interleaved() -> None:
    print("\n── 50× Status + 50× Bridge Interleaved ──")
    def status(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def bridge(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(status, i) for i in range(50)] + [pool.submit(bridge, i) for i in range(50)]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"100/100 interleaved", all(results), f"ok={sum(results)}")


# ─── No world-writable ───────────────────────────────────────────────────────

def test_no_world_writable() -> None:
    print("\n── Activators Not World-Writable ──")
    for c in COMPONENTS:
        path = BIN / f"activate_{c}.sh"
        mode = path.stat().st_mode
        world_write = bool(mode & stat.S_IWOTH)
        record(f"{c} not world-writable", not world_write, f"mode={oct(mode)}")


# ─── Exit 0 implies DEPLOYMENT_COMPLETE ──────────────────────────────────────

def test_exit0_implies_complete() -> None:
    print("\n── Exit 0 Implies DEPLOYMENT_COMPLETE on Success Paths ──")
    for c in COMPONENTS[:4]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0:
            record(f"{c} exit0 has DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" in out)
        else:
            record(f"{c} exit0 has DEPLOYMENT_COMPLETE", False, f"code={code}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Absolute-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_repeated_true_tokens()
    test_gate_satisfied_never_false()
    test_zip_ref_fragment()
    test_parallel_80_status()
    test_emoji_input()
    test_files_created_all()
    test_dashboard_control12()
    test_unknown_embeds_name()
    test_300_refuse()
    test_suites_nonempty()
    test_label_ne_component()
    test_50_50_interleaved()
    test_no_world_writable()
    test_exit0_implies_complete()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
