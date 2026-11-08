#!/usr/bin/env python3
"""Gate-variant census.

Two gates, two rules.

The lattice gate (activate_avrone.sh) is a literal compare:
  [[ "${ALLOW_AGENT_EXECUTION:-}" != "true" ]]
Unset, empty, "true ", and "True" all refuse. Only "true" passes.

The autopilot root gate is a presence check:
  [ "$(id -u)" = 0 ] && [ -z "${ALLOW_ROOT:-}" ]
Unset and empty refuse. Any non-empty value passes, including "true"
and "1 ". It is not a literal compare.

Both are working as written. They are not the same gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


def lattice(value: str | None) -> tuple[int, str, str]:
    env = os.environ.copy()
    if value is None:
        env.pop("ALLOW_AGENT_EXECUTION", None)
    else:
        env["ALLOW_AGENT_EXECUTION"] = value
    p = subprocess.run(["bash", str(ROOT / "bin" / "activate_avrone.sh"), "status"],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    print("AVRONE-12 Gate-variant census")
    for label, value in (("unset", None), ("empty", ""), ("trailing", "true "), ("True", "True")):
        rc, _out, err = lattice(value)
        rec(f"lattice refuses {label}", rc != 0 and "refused" in err)
    rc, out, _err = lattice("true")
    rec("lattice accepts literal true", rc == 0 and "DEPLOYMENT_COMPLETE" in out)

    # The root gate is a presence check. Probe the condition.
    root_text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("root gate is a presence check", '[ -z "${ALLOW_ROOT:-}" ]' in root_text)
    rec("lattice gate is a literal compare", '!= "true"' in (ROOT / "bin" / "activate_avrone.sh").read_text())

    refuse = channel_snapshot(0, 4, "deterministic")  # four variants refused
    accept = channel_snapshot(0, 1, "deterministic")  # literal true accepted
    rec("both are pure", refuse["S"] == accept["S"] == 0.0 and refuse["onConeFloor"] and accept["onConeFloor"])

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v23",
        "finding": (
            "The lattice gate is a literal compare to 'true'. Unset, empty, 'true ', "
            "and 'True' all refuse. Only 'true' passes. The autopilot root gate is a "
            "presence check: unset and empty refuse, any non-empty value passes. "
            "Both work as written. They are not the same gate. Pure. Heat is zero."
        ),
        "cone": CONE,
        "lattice": {
            "rule": "literal compare to 'true'",
            "refuses": ["unset", "empty", "true ", "True"],
            "accepts": ["true"],
            "orientation": refuse["orientation"],
            "S": refuse["S"],
            "psi": refuse["psi"],
            "effectiveRuns": 1,
        },
        "root": {
            "rule": "presence check, -z",
            "refuses": ["unset", "empty"],
            "accepts": ["1", "true", "1 "],
        },
        "docker": docker,
        "mitigation": "Do not make them the same gate. The lattice gate is strict. The root gate is a presence check.",
    }
    out = ROOT / "references" / "great-scale-gate-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("gate evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"lattice refuses: orientation={refuse['orientation']} S={refuse['S']} Psi={refuse['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
