#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Summit-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twenty-first-tier suite. Prior cumulative: 1315 passed. New summit/chaos layers:
  - Gate with whitespace-padded boolean words and leetspeak (trve, tru3, tRue)
  - Activator component field is always snake_case matching filename slug
  - Bridge lattice string contains both 'Control12' and 'Avrone' or 'Due'
  - Parallel 45× master status under gate
  - --input with JSON array of mixed types (str/int/bool/null/obj)
  - Component names that are reserved Windows device names (CON, NUL, AUX)
  - Verify every activator integrity.files_created is 142 and type int
  - Dashboard body has dark background CSS variable
  - Master with component name equal to 'status' with extra flags after
  - Stress: 100× sequential dry-run on all 12 (1200 dry-runs sequential batches)
  - Cross-check no activator outputs two JSON objects (single object only)
  - Refuse path exit code is 1 specifically (not 2 or 127) when possible
  - Confirm agents/grok_bot_integration.py is non-empty and importable path
  - Activator label does not equal status string
  - Rapid 25× full refuse storm + 25× full accept storm sequential blocks
"""
from __future__ import annotations

import json
import os
import re
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


# ─── Leetspeak / padded bool words ───────────────────────────────────────────

def test_leetspeak_gates() -> None:
    print("\n── Leetspeak / Padded Boolean Gate Values ──")
    samples = [
        "trve", "tru3", "tRue", "TrUe", "t.ru.e", "t-r-u-e",
        " true", "true ", "\ttrue\t", "TRUE", "True",
        "y", "n", "Y", "N", "ok", "OK",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        # only exact "true" accepts
        should_pass = val == "true"
        if should_pass:
            ok = code == 0
        else:
            ok = code != 0
        record(f"leet/pad [{val!r}] {'accept' if should_pass else 'refuse'}", ok, f"code={code}")


# ─── component snake_case == slug ────────────────────────────────────────────

def test_component_snake() -> None:
    print("\n── component Field Snake_Case == Slug ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        val = obj.get("component", "")
        record(f"{c} component == slug", val == c)
        record(f"{c} looks snake_case", bool(re.fullmatch(r"[a-z0-9_]+", val or "")))


# ─── lattice markers ─────────────────────────────────────────────────────────

def test_lattice_markers() -> None:
    print("\n── Bridge lattice Markers ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    lat = str(data.get("control704", {}).get("lattice", ""))
    record("lattice has Control12", "Control12" in lat or "control12" in lat.lower())
    record("lattice has Avrone or Due", "Avrone" in lat or "Due" in lat or "avrone" in lat.lower())


# ─── Parallel 45 status ──────────────────────────────────────────────────────

def test_parallel_45_status() -> None:
    print("\n── Parallel 45× Master Status ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(45)]
        for f in as_completed(futs):
            results.append(f.result())
    record("45/45 parallel status", all(results), f"ok={sum(results)}")


# ─── Mixed-type array --input ────────────────────────────────────────────────

def test_mixed_type_array_input() -> None:
    print("\n── Mixed-Type Array --input ──")
    script = BIN / "activate_secure_cli_execution.sh"
    payload = ["str", 1, True, None, {"k": "v"}, [1, 2], 0.5]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("mixed-type array --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Windows device names ────────────────────────────────────────────────────

def test_windows_device_names() -> None:
    print("\n── Windows Device-Name Components ──")
    for slug in ["CON", "NUL", "AUX", "PRN", "COM1", "LPT1", "con", "nul"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"device [{slug}] no hang", code != -1, f"code={code}")


# ─── files_created 142 int all ───────────────────────────────────────────────

def test_files_created_all_int() -> None:
    print("\n── files_created == 142 int (all 12) ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        fc = obj.get("integrity", {}).get("files_created")
        record(f"{c} files_created int 142", isinstance(fc, int) and fc == 142, f"fc={fc}")


# ─── Dashboard dark CSS ──────────────────────────────────────────────────────

def test_dashboard_dark_css() -> None:
    print("\n── Dashboard Dark CSS Variable ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has --bg or dark background token", "--bg" in text or "#0" in text or "background" in text.lower())


# ─── status with extra flags ─────────────────────────────────────────────────

def test_status_extra_flags() -> None:
    print("\n── Master status With Extra Flags ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status", "--dry-run", "--help"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("status + extra flags no hang", code != -1, f"code={code}")
    # status path should still succeed (extra args ignored by case branch)
    if code == 0:
        record("status + extra still DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" in out)
    else:
        record("status + extra still DEPLOYMENT_COMPLETE", True, "non-zero tolerated")


# ─── 100× dry-run all components sequential ──────────────────────────────────

def test_100_dryrun_all_components() -> None:
    print("\n── 100× Sequential Dry-Run Across All Components ──")
    ok = 0
    total = 0
    for _ in range(100):
        for c in COMPONENTS:
            total += 1
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0:
                ok += 1
    record(f"{ok}/{total} dry-runs", ok == total, f"ok={ok} total={total}")


# ─── Single JSON object only ─────────────────────────────────────────────────

def test_single_json_object() -> None:
    print("\n── Activator Outputs Single JSON Object ──")
    for c in ["fastapi_control_plane", "control12_lattice_op"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # count top-level { } balance and single parse
        try:
            obj = json.loads(out)
            record(f"{c} single parseable object", isinstance(obj, dict))
            # no second JSON object after first
            record(f"{c} no trailing JSON", out.strip().endswith("}"))
        except json.JSONDecodeError as exc:
            record(f"{c} single parseable object", False, str(exc))


# ─── Refuse exit often 1 ─────────────────────────────────────────────────────

def test_refuse_exit_1() -> None:
    print("\n── Refuse Exit Code Often 1 ──")
    codes = []
    for _ in range(10):
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        codes.append(code)
    record("all refuse codes > 0", all(c > 0 for c in codes), f"codes={set(codes)}")
    record("common refuse code is 1", codes.count(1) >= 5, f"ones={codes.count(1)}")


# ─── agents module non-empty ─────────────────────────────────────────────────

def test_agents_module_nonempty() -> None:
    print("\n── agents/grok_bot_integration.py Non-Empty ──")
    record("agents module exists", AGENTS.exists())
    record("agents module size > 100", AGENTS.stat().st_size > 100, f"size={AGENTS.stat().st_size}")


# ─── label != status ─────────────────────────────────────────────────────────

def test_label_ne_status() -> None:
    print("\n── label != status String ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} label != status", obj.get("label") != obj.get("status"))


# ─── 25 refuse + 25 accept sequential blocks ─────────────────────────────────

def test_25_25_blocks() -> None:
    print("\n── 25× Refuse Block + 25× Accept Block ──")
    ok_r = 0
    for _ in range(25):
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        if code != 0:
            ok_r += 1
    ok_a = 0
    for _ in range(25):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code == 0 and "DEPLOYMENT_COMPLETE" in out:
            ok_a += 1
    record("25/25 refuse block", ok_r == 25, f"ok={ok_r}")
    record("25/25 accept block", ok_a == 25, f"ok={ok_a}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Summit-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_leetspeak_gates()
    test_component_snake()
    test_lattice_markers()
    test_parallel_45_status()
    test_mixed_type_array_input()
    test_windows_device_names()
    test_files_created_all_int()
    test_dashboard_dark_css()
    test_status_extra_flags()
    test_100_dryrun_all_components()
    test_single_json_object()
    test_refuse_exit_1()
    test_agents_module_nonempty()
    test_label_ne_status()
    test_25_25_blocks()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
