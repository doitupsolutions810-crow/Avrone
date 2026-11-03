#!/usr/bin/env python3
"""One-checkout launch and live verification. Uses only Python's standard library."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.grok"


def preflight():
    checks = {"python": sys.version_info >= (3, 11), "docker_cli": bool(shutil.which("docker")),
              "compose": False, "docker_daemon": False}
    if checks["docker_cli"]:
        for key, command in [("compose", ["docker", "compose", "version"]),
                             ("docker_daemon", ["docker", "info"])]:
            try:
                checks[key] = subprocess.run(command, capture_output=True, timeout=15).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                checks[key] = False
    return {"state": "READY" if all(checks.values()) else "BLOCKED", "checks": checks,
            "sources": [{k: s[k] for k in ("id", "repository", "revision")}
                        for s in json.loads((ROOT / "avrone/static/source-registry.json").read_text())["sources"]]}


def credentials():
    if not ENV.exists():
        fd = os.open(ENV, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("AVRONE_ADMIN_TOKEN=" + secrets.token_urlsafe(36) + "\n")
            f.write("GROK_BRIDGE_TOKEN=" + secrets.token_urlsafe(36) + "\n")
    return dict(line.split("=", 1) for line in ENV.read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def api(path, token, payload=None, timeout=10):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request("http://127.0.0.1:8710" + path, data=body,
                                     headers={"X-Admin-Token": token, "Content-Type": "application/json"})
    # A host proxy must never intercept loopback requests or receive the token.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("response exceeded 2 MiB")
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "up", "verify", "down"])
    parser.add_argument("--open", action="store_true", help="open both consoles after startup")
    parser.add_argument("--swarm", action="store_true", help="enable the separately provisioned Capability Authority + Swarm profile")
    parser.add_argument("--task", default="Independently calculate 17 times 19, explain a verification check and identify any unavailable evidence. Do not perform external writes.")
    args = parser.parse_args(argv)
    report = preflight()
    output = ROOT / "data"
    output.mkdir(exist_ok=True)
    (output / "integration-preflight.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.action == "check" or report["state"] != "READY":
        print(json.dumps({k: v for k, v in report.items() if k != "sources"}, indent=2))
        return 0 if report["state"] == "READY" else 1
    if args.swarm and args.action not in {"up", "down"}:
        parser.error("--swarm is supported only with up and down")
    env = credentials()
    command = ["docker", "compose", "--env-file", str(ENV), "-f", str(ROOT / "compose.grok.yml")]
    if args.swarm:
        required = (
            "AVRONE_SECRET_DIR", "AVRONE_CA_URL", "AVRONE_CA_PUBLIC_KEY_B64",
            "AVRONE_SWARM_URL", "AVRONE_SWARM_ISSUER",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SystemExit("Swarm profile is missing configuration: " + ", ".join(missing))
        secret_dir = Path(os.environ["AVRONE_SECRET_DIR"]).expanduser()
        if secret_dir.is_symlink() or not secret_dir.is_dir() or (secret_dir.stat().st_mode & 0o077):
            raise SystemExit("AVRONE_SECRET_DIR must be a non-symlink mode-0700 directory")
        expected_files = (
            "avrone-ca-requester.pem", "avrone-ca-requester.pub",
            "avrone-swarm-issuer.pem", "avrone-swarm-issuer.pub",
            "avrone-swarm-parent.json", "swarm-receipt.pub",
        )
        for name in expected_files:
            path = secret_dir / name
            if path.is_symlink() or not path.is_file() or (path.stat().st_mode & 0o077):
                raise SystemExit("missing, symlinked, or permissive Swarm credential: " + name)
        command += ["-f", str(ROOT / "compose.swarm.override.yml")]
    if args.action == "down":
        return subprocess.run(command + ["down"], cwd=ROOT).returncode
    if args.action == "up":
        subprocess.run(command + ["config", "--quiet"], cwd=ROOT, check=True)
        subprocess.run(command + ["up", "-d", "--build", "--wait", "--wait-timeout", "3600"], cwd=ROOT, check=True)
        print("Services started. AVRONE: http://127.0.0.1:8710/integration · Grok Bot: http://127.0.0.1:3000")
        print("Use AVRONE_ADMIN_TOKEN from .env.grok in AVRONE's admin field. Run this script with verify to execute the live council.")
        if args.open:
            webbrowser.open("http://127.0.0.1:8710/integration")
            webbrowser.open("http://127.0.0.1:3000")
        return 0
    status = api("/api/integration", env["AVRONE_ADMIN_TOKEN"])
    if status["state"] != "CONNECTED":
        print(json.dumps(status, indent=2))
        return 1
    receipt = api("/api/integration/conclude", env["AVRONE_ADMIN_TOKEN"],
                  {"message": args.task, "squad_size": 2}, timeout=1800)
    (output / "integration-live-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({k: receipt[k] for k in ["run_id", "state", "answer", "receipt_sha256"]}, indent=2))
    return 0 if receipt["state"] == "COMPLETE" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as e:
        print("Launch/verification failed: " + type(e).__name__, file=sys.stderr)
        sys.exit(1)
