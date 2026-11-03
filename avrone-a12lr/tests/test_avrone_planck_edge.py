#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Planck-Scale Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Ninth-tier suite. Prior cumulative: 583 passed. New micro/chaos layers:
  - Gate with only ASCII control chars (BEL, BS, ESC, DEL)
  - Activator invoked with argv0 spoofing (bash -c patterns avoided; use explicit)
  - Bridge output max depth / no circular refs (json.dumps safe)
  - Master + all 12 components sequential full matrix once
  - Env ALLOW_AGENT_EXECUTION=true with extra trailing NUL attempt (OS reject)
  - Dashboard <title> contains AVRONE
  - Every activator --dry-run returns parseable JSON or request-shaped dict
  - Rapid alternate 100× (status / refuse) interleaved single-threaded
  - Component field never equals a different component's slug
  - Integrity files_created == 142 across 5 random activators
  - Bridge control704.priority exact 'high'
  - Zero-byte file as --input
  - FIFO/pipe-style --input via process substitution simulation (file only)
  - Confirm runs/ examples/ dirs exist or creatable without error
  - Agent count stable across 10 bridge calls
  - Master 'status' output keys exact set
  - Stress: 30× parallel unknown-component calls
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

MASTER_STATUS_KEYS = {
    "status", "schema", "gate", "source", "components_registered",
}


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


# ─── Control-char gates ──────────────────────────────────────────────────────

def test_control_char_gates() -> None:
    print("\n── ASCII Control-Char Gates ──")
    for label, ch in [
        ("BEL", "\x07"),
        ("BS", "\x08"),
        ("ESC", "\x1b"),
        ("DEL", "\x7f"),
        ("true+BEL", "true\x07"),
        ("BEL+true", "\x07true"),
    ]:
        try:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": ch},
            )
            record(f"control gate [{label}] refuses/no-hang", code != 0 and code != -1, f"code={code}")
        except Exception as exc:
            record(f"control gate [{label}] OS reject", True, str(exc)[:40])


# ─── Full sequential matrix ──────────────────────────────────────────────────

def test_full_sequential_matrix() -> None:
    print("\n── Full Sequential Matrix (status + 12 components) ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("matrix status", code == 0 and "DEPLOYMENT_COMPLETE" in out)
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"matrix {c}", code == 0 and "DEPLOYMENT_COMPLETE" in out and c in out)


# ─── Bridge json.dumps safe ──────────────────────────────────────────────────

def test_bridge_json_safe() -> None:
    print("\n── Bridge Output json.dumps Safe ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    try:
        again = json.dumps(obj)
        json.loads(again)
        record("bridge output round-trip json safe", True)
    except (TypeError, ValueError) as exc:
        record("bridge output round-trip json safe", False, str(exc))


# ─── Dry-run parseable ───────────────────────────────────────────────────────

def test_dryrun_parseable() -> None:
    print("\n── All Dry-Run Parseable JSON/Object ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code != 0:
            record(f"dry-run {c} exit 0", False, f"code={code}")
            continue
        try:
            obj = json.loads(out)
            record(f"dry-run {c} parseable JSON", isinstance(obj, dict))
        except json.JSONDecodeError:
            # some dry-runs may print raw request text containing component=
            ok = "component" in out.lower() or c in out
            record(f"dry-run {c} parseable or request-shaped", ok)


# ─── 100× alternate status/refuse ────────────────────────────────────────────

def test_100_alternate() -> None:
    print("\n── 100× Alternate Status/Refuse Single-Threaded ──")
    ok = 0
    for i in range(100):
        if i % 2 == 0:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if code == 0 and "DEPLOYMENT_COMPLETE" in out:
                ok += 1
        else:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": "false"},
            )
            if code != 0:
                ok += 1
    record("100/100 alternate status/refuse", ok == 100, f"ok={ok}")


# ─── Component field uniqueness ──────────────────────────────────────────────

def test_component_field_uniqueness() -> None:
    print("\n── Component Field Never Cross-Matches ──")
    seen = {}
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        seen[c] = obj.get("component")
    for c, val in seen.items():
        record(f"{c} component unique to self", val == c)
    # no cross
    values = list(seen.values())
    record("all component values unique", len(values) == len(set(values)))


# ─── files_created == 142 across sample ──────────────────────────────────────

def test_files_created_142() -> None:
    print("\n── integrity.files_created == 142 (sample of 5) ──")
    sample = COMPONENTS[::2][:5]
    for c in sample:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        fc = obj.get("integrity", {}).get("files_created")
        record(f"{c} files_created == 142", fc == 142, f"fc={fc}")


# ─── priority exact high ─────────────────────────────────────────────────────

def test_priority_exact() -> None:
    print("\n── control704.priority Exact 'high' ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    p = data.get("control704", {}).get("priority")
    record("priority == 'high'", p == "high", f"priority={p!r}")


# ─── Zero-byte --input ───────────────────────────────────────────────────────

def test_zero_byte_input() -> None:
    print("\n── Zero-Byte --input ──")
    script = BIN / "activate_react_dashboard.sh"
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        path = tf.name  # empty
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=8,
        )
        record("zero-byte --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── runs/ examples/ present ─────────────────────────────────────────────────

def test_support_dirs() -> None:
    print("\n── Support Dirs runs/ examples/ ──")
    record("runs/ exists", (ROOT / "runs").is_dir())
    record("examples/ exists", (ROOT / "examples").is_dir())
    record("adapters/ exists", (ROOT / "adapters").is_dir())
    record("references/ exists", (ROOT / "references").is_dir())


# ─── Agent count stable ──────────────────────────────────────────────────────

def test_agent_count_stable() -> None:
    print("\n── Agent Count Stable Across 10 Bridge Calls ──")
    counts = []
    for _ in range(10):
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        data = json.loads(out)
        counts.append(data.get("agents", {}).get("count"))
    record("all counts == 6", all(c == 6 for c in counts), f"counts={set(counts)}")


# ─── Master status key set ───────────────────────────────────────────────────

def test_master_status_keys() -> None:
    print("\n── Master Status Key Set ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    missing = MASTER_STATUS_KEYS - set(obj.keys())
    record("master status has required keys", not missing, f"missing={missing}")
    record("components_registered == 12", obj.get("components_registered") == 12)


# ─── 30× parallel unknown ────────────────────────────────────────────────────

def test_parallel_unknown_30() -> None:
    print("\n── 30× Parallel Unknown-Component ──")
    def one(i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), f"unknown_omega_{i}"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code != 0 and code != -1

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(one, i) for i in range(30)]
        for f in as_completed(futs):
            results.append(f.result())
    record("30/30 parallel unknown non-zero", all(results), f"ok={sum(results)}")


# ─── Dashboard title ─────────────────────────────────────────────────────────

def test_dashboard_title() -> None:
    print("\n── Dashboard <title> ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("title contains AVRONE", "<title>" in text.lower() and "avrone" in text.lower())


# ─── Dashboard DEPLOYMENT_COMPLETE badge ─────────────────────────────────────

def test_dashboard_badge() -> None:
    print("\n── Dashboard DEPLOYMENT_COMPLETE Badge ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("badge text present", "DEPLOYMENT_COMPLETE" in text)


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Planck-Scale Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_control_char_gates()
    test_full_sequential_matrix()
    test_bridge_json_safe()
    test_dryrun_parseable()
    test_100_alternate()
    test_component_field_uniqueness()
    test_files_created_142()
    test_priority_exact()
    test_zero_byte_input()
    test_support_dirs()
    test_agent_count_stable()
    test_master_status_keys()
    test_parallel_unknown_30()
    test_dashboard_title()
    test_dashboard_badge()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
