"""Command line for the built-in supervisor.

`run` stays in the foreground and is what a pid-1 or tini entrypoint executes.
The other verbs talk to that process over the loopback control API.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import process
from .client import ClientError, api_endpoint, request
from .spec import SpecError, load_config
from .supervisor import SupervisorError, bundle_dir, default_state_dir, find_repo
from .units import write_units


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ClientError, SpecError, SupervisorError, OSError, ValueError) as exc:
        print(f"avrone-svc: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avrone-svc", description="AVRONE service supervisor")
    parser.add_argument("--config", type=Path, default=None, help="service file or directory (default: built-in bundle)")
    parser.add_argument("--state-dir", type=Path, default=None, help="durable state directory")
    parser.add_argument("--api-port", type=int, default=8799)
    parser.add_argument("--tick", type=float, default=0.2)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the supervisor in the foreground (pid 1 safe)")
    run.set_defaults(func=_cmd_run)

    start = sub.add_parser("start", help="ensure the supervisor is up and start services")
    start.add_argument("names", nargs="*")
    start.set_defaults(func=_cmd_start)

    stop = sub.add_parser("stop", help="stop services; with no names, stop the supervisor too")
    stop.add_argument("names", nargs="*")
    stop.set_defaults(func=_cmd_stop)

    restart = sub.add_parser("restart", help="restart services")
    restart.add_argument("names", nargs="*")
    restart.set_defaults(func=_cmd_restart)

    status = sub.add_parser("status", help="show service status")
    status.set_defaults(func=_cmd_status)

    logs = sub.add_parser("logs", help="print a service log")
    logs.add_argument("name")
    logs.add_argument("-n", "--lines", type=int, default=200)
    logs.set_defaults(func=_cmd_logs)

    tail = sub.add_parser("tail", help="follow a service log")
    tail.add_argument("name")
    tail.set_defaults(func=_cmd_tail)

    reload_p = sub.add_parser("reload", help="reload service definitions (also SIGHUP)")
    reload_p.set_defaults(func=_cmd_reload)

    enable = sub.add_parser("enable", help="enable and start a service")
    enable.add_argument("name")
    enable.set_defaults(func=_cmd_enable)

    disable = sub.add_parser("disable", help="disable and stop a service")
    disable.add_argument("name")
    disable.set_defaults(func=_cmd_disable)

    check = sub.add_parser("check", help="validate definitions and report missing binaries")
    check.set_defaults(func=_cmd_check)

    units = sub.add_parser("units", help="write optional systemd or launchd units")
    units.add_argument("kind", choices=("systemd", "launchd"))
    units.add_argument("-o", "--out", type=Path, required=True)
    units.set_defaults(func=_cmd_units)
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    from .supervisor import Supervisor
    supervisor = Supervisor(
        config=_config(args),
        state_dir=_state(args),
        api_port=args.api_port,
        tick=args.tick,
        handle_signals=True,
        subreaper=True,
    )
    return supervisor.run()


def _cmd_start(args: argparse.Namespace) -> int:
    _ensure_supervisor(args)
    if args.names:
        for name in args.names:
            _print_service(request(_state(args), "POST", f"/v1/services/{name}/start"))
    else:
        body = request(_state(args), "POST", "/v1/start")
        _print_status(body)
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    state = _state(args)
    if args.names:
        for name in args.names:
            _print_service(request(state, "POST", f"/v1/services/{name}/stop"))
        return 0
    try:
        request(state, "POST", "/v1/shutdown", {"mark_stopped": True})
    except ClientError as exc:
        text = str(exc)
        if "unreachable" in text or "not running" in text:
            print("supervisor stopped")
            return 0
        raise
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            api_endpoint(state)
        except ClientError:
            print("supervisor stopped")
            return 0
        time.sleep(0.1)
    print("avrone-svc: supervisor did not exit", file=sys.stderr)
    return 1


def _cmd_restart(args: argparse.Namespace) -> int:
    state = _state(args)
    _ensure_supervisor(args)
    names = args.names or [row["name"] for row in request(state, "GET", "/v1/services")["services"]]
    for name in names:
        _print_service(request(state, "POST", f"/v1/services/{name}/restart"))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _print_status(request(_state(args), "GET", "/v1/status"))
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    body = request(_state(args), "GET", f"/v1/services/{args.name}/logs?lines={args.lines}")
    for line in body.get("lines") or []:
        print(line)
    return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    seen = 0
    while True:
        body = request(_state(args), "GET", f"/v1/services/{args.name}/logs?lines=500")
        lines = body.get("lines") or []
        if seen > len(lines):
            seen = 0
        for line in lines[seen:]:
            print(line, flush=True)
        seen = len(lines)
        time.sleep(0.4)


def _cmd_reload(args: argparse.Namespace) -> int:
    _print_status(request(_state(args), "POST", "/v1/reload"))
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    _print_service(request(_state(args), "POST", f"/v1/services/{args.name}/enable"))
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    _print_service(request(_state(args), "POST", f"/v1/services/{args.name}/disable"))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    specs = load_config(_config(args))
    repo = find_repo(_config(args))
    mapping = {"python": sys.executable, "repo": str(repo), "state": str(_state(args))}
    problems = 0
    for spec in specs:
        spec = spec.substitute(mapping)
        command = spec.command
        override = os.environ.get(spec.command_env, "").strip() if spec.command_env else ""
        if override:
            import shlex
            command = shlex.split(override)
        exists = process.binary_exists(command[0]) if command else False
        if exists:
            state = "ready-to-start" if spec.enabled else "disabled"
        elif spec.optional:
            state = "unavailable"
        else:
            state = "missing"
            problems += 1
        print(f"{spec.name:24} {state:16} {command[0] if command else ''}")
    return 1 if problems else 0


def _cmd_units(args: argparse.Namespace) -> int:
    specs = [spec.substitute({
        "python": sys.executable,
        "repo": str(find_repo(_config(args))),
        "state": str(_state(args)),
    }) for spec in load_config(_config(args))]
    written = write_units(specs, args.kind, args.out)
    for path in written:
        print(path)
    return 0


def _ensure_supervisor(args: argparse.Namespace) -> None:
    state = _state(args)
    try:
        api_endpoint(state)
        return
    except ClientError:
        pass
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / "supervisor.log"
    log = open(log_path, "ab", buffering=0)
    command = [
        sys.executable, "-m", "avrone.svcman", "run",
        "--config", str(_config(args)),
        "--state-dir", str(state),
        "--api-port", str(args.api_port),
        "--tick", str(args.tick),
    ]
    subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(find_repo(_config(args))),
    )
    deadline = time.monotonic() + 15
    last = "supervisor did not become ready"
    while time.monotonic() < deadline:
        try:
            api_endpoint(state)
            return
        except ClientError as exc:
            last = str(exc)
        time.sleep(0.1)
    tail = ""
    if log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise SupervisorError(last + (f"\n{tail}" if tail else ""))


def _config(args: argparse.Namespace) -> Path:
    if args.config:
        return Path(args.config)
    env = os.environ.get("AVRONE_SVC_CONFIG", "").strip()
    return Path(env) if env else bundle_dir()


def _state(args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir)
    return default_state_dir()


def _print_status(body: dict) -> None:
    print(f"supervisor pid {body.get('pid')}  {body.get('api', '')}")
    print(f"{'NAME':24} {'PHASE':16} {'PID':8} {'RESTARTS':8} DETAIL")
    for row in body.get("services") or []:
        _print_service(row)


def _print_service(row: dict) -> None:
    pid = "-" if not row.get("pid") else str(row["pid"])
    detail = row.get("reason") or ""
    print(f"{row.get('name', ''):24} {row.get('phase', ''):16} {pid:8} {row.get('restarts', 0):<8} {detail}")


if __name__ == "__main__":
    sys.exit(main())
