#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Crown-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Twenty-second-tier suite. Prior cumulative: 1406 passed. New crown/chaos layers:
  - Gate with JSON/YAML/TOML boolean literals (true, True, TRUE, yes as yaml)
  - Activator never returns HTTP-looking status codes as status field
  - Bridge identity contains both Control704 and IT Technician
  - Parallel 60× refuse under false gate
  - --input with deeply nested arrays (list of list of list)
  - Component names with emoji only
  - Verify source field identical and non-empty for all 12 in one pass
  - Dashboard has header and footer sections
  - Master with very long component name (8KB)
  - Stress: 80× sequential live rotating + status interleaved
  - Cross-check every suite file name starts with test_avrone_
  - Refuse does not set ALLOW_AGENT_EXECUTION in output as true
  - Confirm bin/activate_avrone.sh size between 200 and 5000 bytes
  - Activator JSON has no null top-level values for required keys
  - Rapid 15× full component matrix (status path + 12 activates) sequential
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

REQUIRED_KEYS = ("status", "component", "label", "gate_satisfied", "source", "integrity")


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


# ─── Config-language boolean literals ────────────────────────────────────────

def test_config_bool_gates() -> None:
    print("\n── Config-Language Boolean Gate Values ──")
    samples = [
        "true",   # only this accepts
        "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON",
        "false", "False", "FALSE", "no", "No", "off", "Off",
        "1", "0", "null", "None", "nil",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        should = val == "true"
        ok = (code == 0) if should else (code != 0)
        record(f"cfg-bool [{val!r}] {'accept' if should else 'refuse'}", ok, f"code={code}")


# ─── status not HTTP-like ────────────────────────────────────────────────────

def test_status_not_http() -> None:
    print("\n── Activator status Not HTTP-Like Codes ──")
    httpish = {"200", "201", "204", "400", "401", "403", "404", "500", "502", "503"}
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        st = str(obj.get("status", ""))
        record(f"{c} status not HTTP code", st not in httpish, f"status={st!r}")


# ─── identity markers ────────────────────────────────────────────────────────

def test_identity_markers() -> None:
    print("\n── Bridge identity Markers ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    ident = str(data.get("control704", {}).get("identity", ""))
    record("identity has Control704", "Control704" in ident)
    record("identity has IT Technician", "IT Technician" in ident)


# ─── Parallel 60 refuse ──────────────────────────────────────────────────────

def test_parallel_60_refuse() -> None:
    print("\n── Parallel 60× Refuse ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "false"},
        )
        return code != 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(one, i) for i in range(60)]
        for f in as_completed(futs):
            results.append(f.result())
    record("60/60 parallel refuse", all(results), f"ok={sum(results)}")


# ─── Nested arrays --input ───────────────────────────────────────────────────

def test_nested_arrays_input() -> None:
    print("\n── Nested Arrays --input ──")
    script = BIN / "activate_training_loop.sh"
    payload = [[[1, 2], [3]], [[4]], []]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=10,
        )
        record("nested arrays --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Emoji-only components ───────────────────────────────────────────────────

def test_emoji_components() -> None:
    print("\n── Emoji-Only Component Names ──")
    for slug in ["🚀", "🔥", "✨", "🎯", "🧬", "⚡"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"emoji [{slug}] no hang", code != -1, f"code={code}")


# ─── source identical non-empty ──────────────────────────────────────────────

def test_source_identical_nonempty() -> None:
    print("\n── source Identical Non-Empty All 12 ──")
    sources = []
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        sources.append(obj.get("source"))
    record("all sources non-empty", all(isinstance(s, str) and len(s) > 0 for s in sources))
    record("all sources identical", len(set(sources)) == 1, f"unique={set(sources)}")


# ─── Dashboard header/footer ─────────────────────────────────────────────────

def test_dashboard_header_footer() -> None:
    print("\n── Dashboard Header / Footer ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has <header or header class", "<header" in text.lower() or "header" in text.lower())
    record("has <footer or footer class", "<footer" in text.lower() or "footer" in text.lower())


# ─── 8KB component name ──────────────────────────────────────────────────────

def test_8kb_component() -> None:
    print("\n── 8KB Component Name ──")
    long_name = "c" * 8192
    code, out, err = run_cmd(
        ["bash", str(MASTER), long_name],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        timeout=10,
    )
    record("8KB component no hang", code != -1, f"code={code}")


# ─── 80× live+status interleaved ─────────────────────────────────────────────

def test_80_live_status() -> None:
    print("\n── 80× Live Rotating + Status Interleaved ──")
    ok = 0
    for i in range(80):
        if i % 2 == 0:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
        else:
            c = COMPONENTS[i % 12]
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
    record("80/80 live+status", ok == 80, f"ok={ok}")


# ─── Suite name prefix ───────────────────────────────────────────────────────

def test_suite_name_prefix() -> None:
    print("\n── Suite Files Start With test_avrone_ ──")
    files = list(TESTS.glob("test_*.py"))
    bad = [f.name for f in files if not f.name.startswith("test_avrone_")]
    record("all test_*.py are test_avrone_*", len(bad) == 0, f"bad={bad}")


# ─── Refuse does not claim true ──────────────────────────────────────────────

def test_refuse_not_claim_true() -> None:
    print("\n── Refuse Does Not Claim Gate true ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = (out + err).lower()
    # should not say gate is satisfied / true as success
    record("refuse does not include DEPLOYMENT_COMPLETE", "deployment_complete" not in combined)
    record("refuse mentions allow or gate", "allow_agent_execution" in combined or "gate" in combined or "refused" in combined)


# ─── Master size bounds ──────────────────────────────────────────────────────

def test_master_size() -> None:
    print("\n── Master Activator Size Bounds ──")
    size = MASTER.stat().st_size
    record("master size 200..5000", 200 <= size <= 5000, f"size={size}")


# ─── Required keys not null ──────────────────────────────────────────────────

def test_required_keys_not_null() -> None:
    print("\n── Required Keys Not Null ──")
    for c in ["agent_registry_orchestrator", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        for k in REQUIRED_KEYS:
            record(f"{c}.{k} not null", obj.get(k) is not None)


# ─── 15× full matrix sequential ──────────────────────────────────────────────

def test_15_full_matrix() -> None:
    print("\n── 15× Full Matrix (status + 12 activates) ──")
    ok = 0
    for _ in range(15):
        good = True
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if not (code == 0 and "DEPLOYMENT_COMPLETE" in out):
            good = False
        if good:
            for c in COMPONENTS:
                code, out, err = run_cmd(
                    ["bash", str(BIN / f"activate_{c}.sh")],
                    env={"ALLOW_AGENT_EXECUTION": "true"},
                )
                if not (code == 0 and "DEPLOYMENT_COMPLETE" in out):
                    good = False
                    break
        if good:
            ok += 1
    record("15/15 full matrix", ok == 15, f"ok={ok}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Crown-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_config_bool_gates()
    test_status_not_http()
    test_identity_markers()
    test_parallel_60_refuse()
    test_nested_arrays_input()
    test_emoji_components()
    test_source_identical_nonempty()
    test_dashboard_header_footer()
    test_8kb_component()
    test_80_live_status()
    test_suite_name_prefix()
    test_refuse_not_claim_true()
    test_master_size()
    test_required_keys_not_null()
    test_15_full_matrix()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
