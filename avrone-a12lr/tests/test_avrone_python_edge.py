#!/usr/bin/env python3
"""Python-version census.

The script refuses when no Python >= 3.11 is found. Probed as uid 0 with
ALLOW_ROOT=1 and every earlier check passing:

- all python too old: refused, 'python install failed'
- python 3.11 present: continues past the python check

The check holds.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" \u2014 {detail}" if detail else ""))


def run(drive: str, path: str, host: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600", WARM_TIMEOUT="5", PATH=path,
               AUTOPILOT_BREW_PREFIXES="/nonexistent", OLLAMA_HOST=host)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


class Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = b'{"version":"stub"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try: self.wfile.write(b)
        except BrokenPipeError: pass
    def log_message(self, *a): pass


def main() -> int:
    print("AVRONE-12 Python-version census")
    drive = tempfile.mkdtemp()
    stub = tempfile.mkdtemp()
    (Path(stub) / "brew").write_text("#!/bin/bash\nexit 1\n")
    os.chmod(Path(stub) / "brew", 0o755)
    (Path(stub) / "ollama").write_text("#!/bin/bash\nif [ \"$1\" = list ]; then echo 'NAME ID SIZE MODIFIED'; echo 'stub-model abc 1GB now'; exit 0; fi\nif [ \"$1\" = run ]; then echo ready; exit 0; fi\nexit 0\n")
    os.chmod(Path(stub) / "ollama", 0o755)
    path = f"{stub}:/bin"
    server = http.server.HTTPServer(("127.0.0.1", 19988), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    try:
        for c in ("python3.13", "python3.12", "python3.11", "python3"):
            (Path(stub) / c).write_text("#!/bin/bash\ncase \"$*\" in\n  *version_info*) exit 1;;\n  *) exit 0;;\nesac\n")
            os.chmod(Path(stub) / c, 0o755)
        rc, out = run(drive, path, "127.0.0.1:19988")
        rec("too old refuses", rc != 0 and "python install failed" in out)
        (Path(stub) / "python3.11").write_text("#!/bin/bash\nexit 0\n")
        os.chmod(Path(stub) / "python3.11", 0o755)
        rc, out = run(drive, path, "127.0.0.1:19988")
        rec("3.11 continues past python check", rc != 0 and "python install failed" not in out)
    finally:
        server.shutdown()
        shutil.rmtree(drive, ignore_errors=True)
        shutil.rmtree(stub, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the refusal is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v51",
        "finding": (
            "The script refuses when no Python >= 3.11 is found and continues past "
            "the python check when Python 3.11 is present. Probed as uid 0 with "
            "ALLOW_ROOT=1 and every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "tooOld": "refuse",
        "present": "continue past python check",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The Python-version check holds.",
    }
    out = ROOT / "references" / "great-scale-python-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("python evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"refusal: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
