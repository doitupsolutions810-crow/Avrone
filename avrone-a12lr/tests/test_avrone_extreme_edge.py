#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Extreme Edge-Case & Adversarial Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Extends prior suites with:
  - Adversarial gate values (null bytes, unicode, injection-like strings)
  - Activator output schema strictness
  - Concurrent-ish parallel subprocess bursts
  - Environment pollution / inherited vars
  - Large / binary / malformed --input payloads
  - Repeated dry-run + live interleaving
  - Bridge field-level type checks
  - Canonical deep-key presence
  - Script shebang / interpreter resilience
  - Working-directory and symlink-style path checks
  - Output determinism across repeated runs
  - Refusal message structure
  - Master activator argument injection attempts
  - Agent lattice / role consistency matrix
  - Dashboard static asset presence
  - Timeout / hang detection on all primary paths
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
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


# ─── Canonical deep integrity ────────────────────────────────────────────────

def test_canonical_deep() -> None:
    print("\n── Canonical Deep Integrity ──")
    if not CANONICAL.exists():
        record("canonical exists", False)
        return
    data = json.loads(CANONICAL.read_text())
    record("schema exact", data.get("schema") == "kg.production-training-result/v2")
    record("status exact", data.get("status") == "DEPLOYMENT_COMPLETE")
    record("timestamp present", bool(data.get("timestamp")))
    bc = data.get("build_context", {})
    record("build_context.system_name", bool(bc.get("system_name")))
    record("build_context.version", bool(bc.get("version")))
    comps = bc.get("components", [])
    record("components is list", isinstance(comps, list))
    record("components count >= 12", len(comps) >= 12, f"n={len(comps)}")
    # deep sample of expected component names
    joined = " ".join(comps).lower()
    for needle in ["fastapi", "react", "langgraph", "control12", "memory"]:
        record(f"components mention '{needle}'", needle in joined)


# ─── Adversarial gate values ─────────────────────────────────────────────────

ADVERSARIAL_GATES = [
    ("null-like", "null"),
    ("None", "None"),
    ("true\\n", "true\n"),
    ("unicode true", "ｔｒｕｅ"),  # fullwidth
    ("injection ; true", "; true"),
    ("true; rm -rf", "true; echo pwned"),
    ("$true", "$true"),
    ("`true`", "`true`"),
    ("true true", "true true"),
    ("TRUE ", "TRUE "),
    (" true", " true"),
    ("true\t", "true\t"),
    ("yes", "yes"),
    ("on", "on"),
    ("enabled", "enabled"),
    ("1", "1"),
    ("0", "0"),
    ("false", "false"),
    ("", ""),
]


def test_adversarial_gates() -> None:
    print("\n── Adversarial Gate Values ──")
    for label, val in ADVERSARIAL_GATES:
        try:
            code, out, err = run_cmd(
                ["bash", str(MASTER), "status"],
                env={"ALLOW_AGENT_EXECUTION": val},
            )
        except ValueError as exc:
            # embedded null or other OS-level env rejection counts as refuse
            record(f"gate adversarial [{label}]", True, f"OS rejected env ({exc})")
            continue
        should_pass = val == "true"
        if should_pass:
            ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
        else:
            ok = code != 0
        record(f"gate adversarial [{label}]", ok, f"val={val!r} code={code}")

    # explicit null-byte case: OS/Python rejects it — treat as hard refuse
    try:
        run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true\0"},
        )
        record("gate adversarial [true\\0]", False, "null byte unexpectedly accepted")
    except ValueError:
        record("gate adversarial [true\\0]", True, "OS/Python rejected embedded null")


# ─── Activator output schema ─────────────────────────────────────────────────

def test_activator_output_schema() -> None:
    print("\n── Activator Output Schema Strictness ──")
    required_keys = {"status", "component", "label", "gate_satisfied", "source", "integrity"}
    for c in ["agent_registry_orchestrator", "control12_lattice_op", "fastapi_control_plane", "persistent_memory_agent"]:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code != 0:
            record(f"schema {c} exits 0", False, err)
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            record(f"schema {c} valid JSON", False, str(exc))
            continue
        record(f"schema {c} valid JSON", True)
        missing = required_keys - set(data.keys())
        record(f"schema {c} required keys", not missing, f"missing={missing}")
        record(f"schema {c} status DEPLOYMENT_COMPLETE", data.get("status") == "DEPLOYMENT_COMPLETE")
        record(f"schema {c} component matches", data.get("component") == c)
        record(f"schema {c} gate_satisfied True", data.get("gate_satisfied") is True)
        integ = data.get("integrity", {})
        record(f"schema {c} integrity.python_compile_ok", integ.get("python_compile_ok") is True)
        record(f"schema {c} integrity.dependencies_resolved == 100", integ.get("dependencies_resolved") == 100)
        record(f"schema {c} integrity.files_created == 142", integ.get("files_created") == 142)


# ─── Parallel burst ──────────────────────────────────────────────────────────

def test_parallel_burst() -> None:
    print("\n── Parallel Burst (8 concurrent status) ──")
    def one(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, i) for i in range(8)]
        for f in as_completed(futs):
            results.append(f.result())
    record("8/8 parallel status succeed", all(results), f"ok={sum(results)}/8")


# ─── Environment pollution ───────────────────────────────────────────────────

def test_env_pollution() -> None:
    print("\n── Environment Pollution ──")
    polluted = {
        "ALLOW_AGENT_EXECUTION": "true",
        "AI_AGENT_COMMAND": "echo injected",
        "PATH": "/tmp:/usr/bin:/bin",
        "AVRONE_ROOT": "/nonexistent",
        "HOME": "/tmp",
        "LANG": "C",
    }
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env=polluted)
    record("master survives polluted env", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    # pollution must not flip gate when gate is false
    polluted["ALLOW_AGENT_EXECUTION"] = "false"
    code, out, err = run_cmd(["bash", str(MASTER), "status"], env=polluted)
    record("polluted env + false gate still refuses", code != 0)


# ─── Malformed / large / binary input ────────────────────────────────────────

def test_malformed_inputs() -> None:
    print("\n── Malformed / Large / Binary --input ──")
    script = BIN / "activate_agent_registry_orchestrator.sh"

    cases = [
        ("empty file", b""),
        ("invalid json", b"{not json"),
        ("array root", b"[1,2,3]"),
        ("null root", b"null"),
        ("large object", json.dumps({"k": "v" * 5000}).encode()),
        ("binary junk", b"\x00\x01\xff\xfe" * 100),
        ("utf8 bom", b"\xef\xbb\xbf{}"),
    ]
    for label, payload in cases:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(payload)
            path = tf.name
        try:
            code, out, err = run_cmd(
                ["bash", str(script), "--input", path],
                env={"ALLOW_AGENT_EXECUTION": "true"},
                timeout=8,
            )
            # must not timeout (-1) and must not crash the harness
            ok = code != -1
            record(f"--input [{label}] no hang", ok, f"code={code}")
        finally:
            os.unlink(path)


# ─── Dry-run / live interleave ───────────────────────────────────────────────

def test_dry_live_interleave() -> None:
    print("\n── Dry-run / Live Interleave ──")
    script = BIN / "activate_control12_lattice_op.sh"
    sequence = ["--dry-run", None, "--dry-run", None, "--dry-run"]
    for i, flag in enumerate(sequence):
        cmd = ["bash", str(script)]
        if flag:
            cmd.append(flag)
        code, out, err = run_cmd(cmd, env={"ALLOW_AGENT_EXECUTION": "true"})
        if flag:
            ok = code == 0 and ("component" in out or "control12" in out.lower())
            record(f"interleave step {i} dry-run", ok)
        else:
            ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
            record(f"interleave step {i} live", ok)


# ─── Bridge field types ──────────────────────────────────────────────────────

def test_bridge_field_types() -> None:
    print("\n── Bridge Field-Level Types ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    if code != 0:
        record("bridge runs", False, err)
        return
    data = json.loads(out)
    record("bridge.status is str", isinstance(data.get("status"), str))
    record("bridge.schema is str", isinstance(data.get("schema"), str))
    record("bridge.control704 is dict", isinstance(data.get("control704"), dict))
    record("bridge.integration is dict", isinstance(data.get("integration"), dict))
    record("bridge.agents is dict", isinstance(data.get("agents"), dict))
    agents = data["agents"]
    record("agents.count is int", isinstance(agents.get("count"), int))
    record("agents.gate_satisfied is bool", isinstance(agents.get("gate_satisfied"), bool))
    record("agents.agents is list", isinstance(agents.get("agents"), list))
    for a in agents.get("agents", []):
        record(f"agent {a.get('id')} has id str", isinstance(a.get("id"), str))
        record(f"agent {a.get('id')} has role str", isinstance(a.get("role"), str))
        record(f"agent {a.get('id')} has status str", isinstance(a.get("status"), str))


# ─── Refusal message structure ───────────────────────────────────────────────

def test_refusal_structure() -> None:
    print("\n── Refusal Message Structure ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = out + err
    record("refusal contains ALLOW_AGENT_EXECUTION", "ALLOW_AGENT_EXECUTION" in combined)
    # prefer structured JSON if present
    try:
        # may be on stderr
        for stream in (err, out):
            if stream.strip().startswith("{"):
                data = json.loads(stream)
                record("refusal is JSON object", True)
                record("refusal has status refused-ish", "status" in data or "reason" in data)
                break
        else:
            record("refusal is JSON object", False, "no JSON found (acceptable if text)")
    except json.JSONDecodeError:
        record("refusal parseable or text", True, "non-JSON text refusal accepted")


# ─── Master argument injection ───────────────────────────────────────────────

def test_master_arg_injection() -> None:
    print("\n── Master Argument Injection Attempts ──")
    payloads = [
        ["; echo pwned"],
        ["$(echo pwned)"],
        ["`echo pwned`"],
        ["status; ls"],
        ["-h"],
        ["--help"],
        ["../../../etc/passwd"],
        ["status\nstatus"],
    ]
    for args in payloads:
        code, out, err = run_cmd(
            ["bash", str(MASTER)] + args,
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        # must not hang and must not expand/execute injected shell.
        # Note: the literal argument may appear in an unknown_component message;
        # that is safe. We only fail if shell expansion produced extra output
        # beyond the structured JSON refusal/unknown response.
        no_hang = code != -1
        combined = (out + "\n" + err).strip()
        # evidence of actual execution of the injected command
        executed = bool(re.search(r"(?m)^pwned$", combined)) or "pwned\n" in combined
        clean_status = (
            code == 0
            and "DEPLOYMENT_COMPLETE" in out
            and "components_registered" in out
        )
        ok = no_hang and not executed and not clean_status
        record(f"injection args {args!r}", ok, f"code={code} clean={clean_status} executed={executed}")


# ─── Agent consistency matrix ────────────────────────────────────────────────

def test_agent_consistency() -> None:
    print("\n── Agent Lattice / Role Consistency ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    agents = data.get("agents", {}).get("agents", [])
    by_id = {a["id"]: a for a in agents}

    expectations = {
        "grok-bot-supervisor": {"lattice": "Control12", "status": "registered"},
        "grok-bot-computer-use": {"lattice": "Control12", "status": "registered"},
        "grok-bot-github-bridge": {"lattice": "Control12", "status": "registered"},
        "avrone-fastapi-control": {"status": "implementation_ready"},
        "avrone-react-dashboard": {"status": "implementation_ready"},
        "persistent-memory-agent": {"status": "registered"},
    }
    for aid, exp in expectations.items():
        a = by_id.get(aid)
        if not a:
            record(f"agent {aid} present", False)
            continue
        for k, v in exp.items():
            record(f"{aid}.{k} == {v}", a.get(k) == v)


# ─── Determinism ─────────────────────────────────────────────────────────────

def test_output_determinism() -> None:
    print("\n── Output Determinism (3× status) ──")
    outputs = []
    for _ in range(3):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        outputs.append(out)
    record("3 status outputs identical", len(set(outputs)) == 1)


# ─── Dashboard asset ─────────────────────────────────────────────────────────

def test_dashboard_asset() -> None:
    print("\n── Dashboard Static Asset ──")
    record("dashboard/index.html exists", DASHBOARD.exists())
    if DASHBOARD.exists():
        text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
        record("dashboard contains AVRONE", "AVRONE" in text)
        record("dashboard contains Control704", "Control704" in text)
        record("dashboard contains DEPLOYMENT_COMPLETE", "DEPLOYMENT_COMPLETE" in text)
        record("dashboard contains agent table markers", "grok-bot-supervisor" in text)
        record("dashboard is HTML", "<html" in text.lower() and "</html>" in text.lower())


# ─── Shebang / interpreter resilience ────────────────────────────────────────

def test_shebang_resilience() -> None:
    print("\n── Shebang / Interpreter Resilience ──")
    # invoke via explicit bash (already our pattern) and via env if possible
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("explicit bash invocation works", code == 0)

    # scripts should start with #!
    for c in COMPONENTS[:3]:
        head = (BIN / f"activate_{c}.sh").read_text(encoding="utf-8", errors="replace")[:40]
        record(f"activate_{c}.sh has shebang", head.startswith("#!"))


# ─── Working directory variance ──────────────────────────────────────────────

def test_cwd_variance() -> None:
    print("\n── Working Directory Variance ──")
    # run from /tmp (scripts use absolute-ish or relative carefully)
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
        cwd=Path("/tmp"),
    )
    # may fail if relative paths break — still must not hang
    record("master from /tmp does not hang", code != -1, f"code={code}")
    # preferred: still works if ROOT is baked or paths absolute enough
    if code == 0:
        record("master from /tmp still succeeds", True)
    else:
        record("master from /tmp still succeeds", False, "relative path dependency (documented)")


# ─── Timeout baseline ────────────────────────────────────────────────────────

def test_no_hang_baseline() -> None:
    print("\n── No-Hang Baseline (all primary paths < 8s) ──")
    paths = [
        (["bash", str(MASTER), "status"], {"ALLOW_AGENT_EXECUTION": "true"}),
        (["bash", str(BIN / "activate_agent_registry_orchestrator.sh")], {"ALLOW_AGENT_EXECUTION": "true"}),
        (["bash", str(BIN / "activate_control12_lattice_op.sh"), "--dry-run"], {"ALLOW_AGENT_EXECUTION": "true"}),
        ([sys.executable, str(BRIDGE)], {"ALLOW_AGENT_EXECUTION": "true"}),
        ([sys.executable, str(AGENTS)], {"ALLOW_AGENT_EXECUTION": "true"}),
    ]
    for cmd, env in paths:
        t0 = time.monotonic()
        code, out, err = run_cmd(cmd, env=env, timeout=8)
        dt = time.monotonic() - t0
        ok = code != -1 and dt < 8
        record(f"no-hang {' '.join(cmd[-2:])}", ok, f"{dt:.2f}s code={code}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 68)
    print("AVRONE-12 Lattice — Extreme Edge-Case & Adversarial Test Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 68)

    test_canonical_deep()
    test_adversarial_gates()
    test_activator_output_schema()
    test_parallel_burst()
    test_env_pollution()
    test_malformed_inputs()
    test_dry_live_interleave()
    test_bridge_field_types()
    test_refusal_structure()
    test_master_arg_injection()
    test_agent_consistency()
    test_output_determinism()
    test_dashboard_asset()
    test_shebang_resilience()
    test_cwd_variance()
    test_no_hang_baseline()

    print("\n" + "=" * 68)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
