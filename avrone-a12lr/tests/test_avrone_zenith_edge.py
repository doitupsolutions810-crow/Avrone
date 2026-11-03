#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Zenith-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Eighteenth-tier suite. Prior cumulative: 1146 passed. New zenith/chaos layers:
  - Gate with HTML/XML entity-like strings (&true;, &#116;rue)
  - Activator component field length matches slug length
  - Bridge agents.count is exactly 6 (int)
  - Parallel 15× full live sweep of all 12 (180 activations)
  - --input with very long string value (100KB)
  - Component names that are single characters from various ranges
  - Verify files_created type is int
  - Dashboard contains 'kg.production-training-result/v2'
  - Master when given only --dry-run as first arg (unknown)
  - Stress: 250× sequential refuse under false
  - Cross-check every activator integrity block is a dict
  - Refuse does not print DEPLOYMENT_COMPLETE on stdout
  - Confirm references/canonical-deployment.json size > 500 bytes
  - Activator source contains 'canonical'
  - Rapid 30× bridge under gate + 30× bridge without gate interleaved
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


# ─── HTML/entity-like true ───────────────────────────────────────────────────

def test_entity_like_true() -> None:
    print("\n── HTML/XML Entity-Like true Gates ──")
    samples = [
        "&true;",
        "&#116;rue",
        "&#x74;rue",
        "&t;rue",
        "true;",
        ";true",
        "<true>",
        "{true}",
    ]
    for val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"entity [{val!r}] refuses", code != 0, f"code={code}")


# ─── component length == slug ────────────────────────────────────────────────

def test_component_len_slug() -> None:
    print("\n── component Field Length == Slug ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        val = obj.get("component", "")
        record(f"{c} len(component)==len(slug)", len(val) == len(c), f"len={len(val)}")


# ─── agents.count exact 6 ────────────────────────────────────────────────────

def test_agents_count_6() -> None:
    print("\n── Bridge agents.count Exact 6 ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    count = data.get("agents", {}).get("count")
    record("count is int 6", count == 6 and isinstance(count, int), f"count={count!r}")


# ─── 15× full live sweeps parallel ───────────────────────────────────────────

def test_15_live_sweeps_parallel() -> None:
    print("\n── Parallel 15× Full 12-Component Live Sweeps ──")
    def sweep(_i: int) -> bool:
        for c in COMPONENTS:
            code, out, err = run_cmd(
                ["bash", str(BIN / f"activate_{c}.sh")],
                env={"ALLOW_AGENT_EXECUTION": "true"},
            )
            if not (code == 0 and "DEPLOYMENT_COMPLETE" in out):
                return False
        return True

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(sweep, i) for i in range(15)]
        for f in as_completed(futs):
            results.append(f.result())
    record("15/15 live sweeps", all(results), f"ok={sum(results)}")


# ─── 100KB string --input ────────────────────────────────────────────────────

def test_100kb_string_input() -> None:
    print("\n── 100KB String --input ──")
    script = BIN / "activate_openai_model_adapter.sh"
    payload = {"blob": "x" * (100 * 1024)}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=15,
        )
        record("100KB --input no hang", code != -1, f"code={code}")
    finally:
        os.unlink(path)


# ─── Single-char components ──────────────────────────────────────────────────

def test_single_char_components() -> None:
    print("\n── Single-Character Component Names ──")
    for slug in ["a", "Z", "0", "9", "_", "-", ".", "*", "@", "#"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"single-char [{slug!r}] no hang", code != -1, f"code={code}")


# ─── files_created type int ──────────────────────────────────────────────────

def test_files_created_type() -> None:
    print("\n── files_created Type int ──")
    for c in ["react_dashboard", "training_loop", "containerized_deployment"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        fc = obj.get("integrity", {}).get("files_created")
        record(f"{c} files_created is int", isinstance(fc, int) and not isinstance(fc, bool))
        record(f"{c} files_created == 142", fc == 142)


# ─── Dashboard schema string ─────────────────────────────────────────────────

def test_dashboard_schema() -> None:
    print("\n── Dashboard Schema String ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("has kg.production-training-result/v2", "kg.production-training-result/v2" in text)


# ─── --dry-run as component ──────────────────────────────────────────────────

def test_dry_run_as_component() -> None:
    print("\n── Master --dry-run as First Arg (unknown) ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "--dry-run"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("--dry-run as component no hang", code != -1, f"code={code}")
    # should not be clean status
    record("--dry-run not clean status", not (code == 0 and "components_registered" in out))


# ─── 250× refuse ─────────────────────────────────────────────────────────────

def test_250_refuse() -> None:
    print("\n── 250× Sequential Refuse ──")
    ok = 0
    for _ in range(250):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "false"},
        )
        if code != 0:
            ok += 1
    record("250/250 refuse", ok == 250, f"ok={ok}")


# ─── integrity is dict ───────────────────────────────────────────────────────

def test_integrity_is_dict() -> None:
    print("\n── integrity Block Is Dict ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} integrity is dict", isinstance(obj.get("integrity"), dict))


# ─── Refuse no DEPLOYMENT_COMPLETE on stdout ─────────────────────────────────

def test_refuse_no_complete_stdout() -> None:
    print("\n── Refuse No DEPLOYMENT_COMPLETE on stdout ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    record("stdout lacks DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" not in out)


# ─── Canonical size ──────────────────────────────────────────────────────────

def test_canonical_size() -> None:
    print("\n── Canonical File Size ──")
    size = CANONICAL.stat().st_size
    record("canonical size > 500 bytes", size > 500, f"size={size}")


# ─── source contains canonical ───────────────────────────────────────────────

def test_source_contains_canonical() -> None:
    print("\n── Activator source Contains 'canonical' ──")
    for c in ["fastapi_control_plane", "control12_lattice_op", "skill_factory"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        src = str(obj.get("source", ""))
        record(f"{c} source has canonical", "canonical" in src.lower())


# ─── Bridge with/without gate interleaved ────────────────────────────────────

def test_bridge_gate_interleaved() -> None:
    print("\n── 30× Bridge With Gate + 30× Without Gate ──")
    def with_gate(_i: int) -> bool:
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def no_gate(_i: int) -> bool:
        code, out, err = run_cmd([sys.executable, str(BRIDGE)], clear_gate=True)
        # without gate: GATE_PENDING or gate_satisfied false
        if code != 0:
            return False
        try:
            data = json.loads(out)
            return data.get("status") == "GATE_PENDING" or data.get("agents", {}).get("gate_satisfied") is False
        except json.JSONDecodeError:
            return False

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(with_gate, i) for i in range(30)] + [pool.submit(no_gate, i) for i in range(30)]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"60/60 bridge gate interleaved", all(results), f"ok={sum(results)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Zenith-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_entity_like_true()
    test_component_len_slug()
    test_agents_count_6()
    test_15_live_sweeps_parallel()
    test_100kb_string_input()
    test_single_char_components()
    test_files_created_type()
    test_dashboard_schema()
    test_dry_run_as_component()
    test_250_refuse()
    test_integrity_is_dict()
    test_refuse_no_complete_stdout()
    test_canonical_size()
    test_source_contains_canonical()
    test_bridge_gate_interleaved()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
