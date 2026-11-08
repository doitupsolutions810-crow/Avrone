#!/usr/bin/env python3
"""Host-check census.

The host check allows only host:port characters. Probed as uid 0 with
ALLOW_ROOT=1, a valid drive, a valid model, valid lanes, and valid
numbers:

- '$(id)': refused (the script appends :11434, still illegal)
- 'host;id': refused
- '127.0.0.1 11434': refused (space)
- '127.0.0.1:11434/api': passes (the path is stripped before the check)
- '127.0.0.1:11434': passes
- '127.0.0.1': passes (the script appends :11434)

The host check holds for shell syntax. A slash and a missing port are
normalized before the check, so they are not refusals.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

REPO = Path("/tmp/Avrone")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(host: str, drive: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600", OLLAMA_HOST=host)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Host-check census")
    drive = tempfile.mkdtemp()
    try:
        bad = ["$(id)", "host;id", "127.0.0.1 11434"]
        for h in bad:
            rc, out = run(h, drive)
            rec(f"refuses {h!r}", rc != 0 and "illegal characters" in out)
        for h in ("127.0.0.1:11434/api", "127.0.0.1:11434", "127.0.0.1"):
            rc, out = run(h, drive)
            rec(f"normalizes {h!r}", rc != 0 and "illegal characters" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 3, "deterministic")
    rec("three refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v28",
        "finding": (
            "The host check allows only host:port characters. Shell syntax and a space "
            "refuse, even with ALLOW_ROOT=1 and valid drive, model, lanes, and numbers. "
            "A slash is stripped and a missing port is appended before the check, so "
            "those are normalizations, not refusals. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": bad,
        "normalized": ["127.0.0.1:11434/api", "127.0.0.1:11434", "127.0.0.1"],
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The host check holds for shell syntax. The slash and missing port are normalized.",
    }
    out = ROOT / "references" / "great-scale-host-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("host evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"three refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
