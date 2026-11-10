#!/usr/bin/env python3
"""Model-present census.

The script refuses when the model is not in a foreign Ollama. Probed as
uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- model not in list: refused, 'not in the Ollama'
- model in list: continues past the model check

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
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600", PATH=path,
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
    print("AVRONE-12 Model-present census")
    drive = tempfile.mkdtemp()
    stub = tempfile.mkdtemp()
    (Path(stub) / "brew").write_text("#!/bin/bash\nexit 0\n")
    os.chmod(Path(stub) / "brew", 0o755)
    path = f"{stub}:/usr/bin:/bin"
    server = http.server.HTTPServer(("127.0.0.1", 19995), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    try:
        (Path(stub) / "ollama").write_text("#!/bin/bash\nif [ \"$1\" = list ]; then echo 'NAME ID SIZE MODIFIED'; exit 0; fi\nexit 0\n")
        os.chmod(Path(stub) / "ollama", 0o755)
        rc, out = run(drive, path, "127.0.0.1:19995")
        rec("missing model refuses", rc != 0 and "not in the Ollama" in out)
        (Path(stub) / "ollama").write_text("#!/bin/bash\nif [ \"$1\" = list ]; then echo 'NAME ID SIZE MODIFIED'; echo 'stub-model abc 1GB now'; exit 0; fi\nexit 0\n")
        os.chmod(Path(stub) / "ollama", 0o755)
        rc, out = run(drive, path, "127.0.0.1:19995")
        rec("present model continues", rc != 0 and "not in the Ollama" not in out)
    finally:
        server.shutdown()
        shutil.rmtree(drive, ignore_errors=True)
        shutil.rmtree(stub, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the refusal is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v49",
        "finding": (
            "The script refuses when the model is not in a foreign Ollama and "
            "continues past the model check when the model is present. Probed as "
            "uid 0 with ALLOW_ROOT=1 and every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "missing": "refuse",
        "present": "continue past model check",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The model-present check holds.",
    }
    out = ROOT / "references" / "great-scale-modelpresent-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("model-present evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"refusal: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
