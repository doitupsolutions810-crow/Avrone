"""Supervisor tests: crashes, health, cycles, signals, pid 1, logs, resume.

Deadlines below are condition waits. They fail the assertion if the supervisor
never reaches the state; they are not a harness timeout that aborts a passing run.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
DURATIONS: list[dict] = []


@pytest.fixture(scope="session", autouse=True)
def _duration_report():
    yield
    out = Path("/opt/cursor/artifacts")
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "svcman-tests.json").write_text(json.dumps(DURATIONS, indent=2), encoding="utf-8")
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _time_test(request):
    start = time.perf_counter()
    yield
    DURATIONS.append({"test": request.node.name, "seconds": round(time.perf_counter() - start, 3)})


def py(*lines: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(lines)]


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def service(name: str, command: list[str], **opts) -> str:
    lines = [f"[services.{name}]", f"command = {toml_value(command)}"]
    for key, value in opts.items():
        if value is None:
            continue
        lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def health(name: str, **opts) -> str:
    lines = [f"[services.{name}.health]"]
    for key, value in opts.items():
        lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


FAST = dict(
    backoff_initial_seconds=0.05,
    backoff_max_seconds=0.4,
    backoff_multiplier=2,
    jitter=0,
    stop_grace_seconds=0.4,
    start_timeout_seconds=5,
    max_restarts=5,
    restart_window_seconds=30,
    circuit_reset_seconds=30,
    stable_seconds=30,
    log_max_bytes=200_000,
    log_backups=3,
)


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s; last={last!r}")


class Running:
    def __init__(self, tmp: Path, text: str, *, signals=False, subreaper=False, tick=0.05):
        self.tmp = tmp
        self.config = tmp / "services.toml"
        self.config.write_text(text, encoding="utf-8")
        self.state = tmp / "state"
        from avrone.svcman.supervisor import Supervisor
        self.sup = Supervisor(
            self.config,
            self.state,
            api_port=0,
            tick=tick,
            handle_signals=signals,
            subreaper=subreaper,
            repo=ROOT,
        )
        import threading
        self.thread = threading.Thread(target=self.sup.run, name="svcman-test", daemon=True)
        self.thread.start()
        assert self.sup.wait_ready(10), "supervisor did not become ready"
        assert self.thread.is_alive(), self._log()
        self.token = (self.state / "token").read_text(encoding="utf-8").strip()
        info = json.loads((self.state / "api.json").read_text(encoding="utf-8"))
        self.base = f"http://{info['host']}:{info['port']}"

    def req(self, method: str, path: str, payload: dict | None = None, token: str | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise AssertionError(f"{method} {path} -> {exc.code} {body}") from exc

    def service(self, name: str) -> dict:
        return self.req("GET", f"/v1/services/{name}", token=self.token)

    def events(self) -> list[dict]:
        return self.req("GET", "/v1/events?lines=2000", token=self.token)["events"]

    def close(self) -> None:
        if self.thread.is_alive():
            self.sup.request_shutdown(mark_stopped=True)
            self.thread.join(10)
        self._kill_recorded()

    def _kill_recorded(self) -> None:
        path = self.state / "state.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for body in (data.get("services") or {}).values():
            pid = body.get("pid")
            if pid:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass

    def _log(self) -> str:
        path = self.state / "events.jsonl"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-2000:]


@pytest.fixture
def running(tmp_path):
    box = {"obj": None}

    def start(text: str) -> Running:
        obj = Running(tmp_path, text)
        box["obj"] = obj
        return obj

    yield start
    if box["obj"] is not None:
        box["obj"].close()


def spawn_cli(tmp: Path, text: str) -> tuple[subprocess.Popen, Path]:
    config = tmp / "services.toml"
    config.write_text(text, encoding="utf-8")
    state = tmp / "state"
    log = open(tmp / "supervisor.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "avrone.svcman", "run",
            "--config", str(config),
            "--state-dir", str(state),
            "--api-port", "0",
            "--tick", "0.05",
        ],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (state / "api.json").exists() and (state / "token").exists():
            return proc, state
        if proc.poll() is not None:
            raise AssertionError(log_text(tmp / "supervisor.log"))
        time.sleep(0.05)
    raise AssertionError("subprocess supervisor did not write api.json\n" + log_text(tmp / "supervisor.log"))


def stop_cli(proc: subprocess.Popen | None, state: Path) -> None:
    if proc is not None and proc.poll() is None:
        try:
            from avrone.svcman.client import request
            request(state, "POST", "/v1/shutdown", {"mark_stopped": True}, timeout=5)
        except Exception:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(3)
    path = state / "state.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for body in (data.get("services") or {}).values():
            pid = body.get("pid")
            if pid:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass


def log_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-4000:]


def disk_events(state: Path) -> list[dict]:
    path = state / "events.jsonl"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def test_crash_loop_opens_circuit_and_keeps_trace(running):
    script = (
        "import sys; sys.stderr.write('Traceback (most recent call last):\\n"
        "RuntimeError: lane exploded\\n'); sys.exit(1)"
    )
    box = running(service("boom", [sys.executable, "-c", script], restart="always", enabled=True, **{**FAST, "max_restarts": 3}))
    body = wait_for(lambda: box.service("boom") if box.service("boom")["phase"] == "circuit_open" else None, timeout=15)
    assert "circuit open" in body["reason"]
    events = disk_events(box.state)
    assert events, "event log empty"
    assert all(ISO_TS.fullmatch(row["ts"]) for row in events)
    crashes = [row for row in events if row["event"] == "crash" and row.get("service") == "boom"]
    restarts = [row for row in events if row["event"] == "restart" and row.get("service") == "boom"]
    opened = [row for row in events if row["event"] == "circuit_open" and row.get("service") == "boom"]
    assert len(restarts) == 3
    assert len(crashes) >= 4
    assert len(opened) == 1
    assert "lane exploded" in opened[0].get("trace", "")
    assert "RuntimeError" in crashes[-1].get("trace", "")
    delays = [row["delay_seconds"] for row in restarts]
    assert delays[0] < delays[1] < delays[2]
    starts = len([row for row in events if row["event"] == "start" and row.get("service") == "boom"])
    time.sleep(0.6)
    later = disk_events(box.state)
    assert len([row for row in later if row["event"] == "start" and row.get("service") == "boom"]) == starts


def test_flapping_health_restarts(running, tmp_path):
    counter = tmp_path / "flip"
    probe = (
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "sys.exit(0 if (n // 2) % 2 == 0 else 1)\n"
    )
    text = service("flap", py("import time", "time.sleep(60)"), restart="on-failure", enabled=True, **{**FAST, "max_restarts": 20})
    text += health("flap", type="command", command=[sys.executable, "-c", probe], interval_seconds=0.05, timeout_seconds=2, unhealthy_threshold=2, healthy_threshold=1)
    box = running(text)
    def restarted():
        rows = [row for row in disk_events(box.state) if row.get("service") == "flap" and row["event"] in {"health_fail", "restart", "anomaly"}]
        fails = [row for row in rows if row["event"] == "health_fail"]
        kicks = [row for row in rows if row["event"] in {"restart", "anomaly"} and (row["event"] == "restart" or row.get("kind") == "unhealthy")]
        return rows if fails and kicks else None
    rows = wait_for(restarted, timeout=15)
    assert all(ISO_TS.fullmatch(row["ts"]) for row in rows)
    assert box.thread.is_alive()


def test_dependency_cycle_does_not_block_others(running):
    sleep = py("import time", "time.sleep(60)")
    text = (
        service("a", sleep, depends_on=["b"], enabled=True, restart="on-failure", **FAST)
        + service("b", sleep, depends_on=["a"], enabled=True, restart="on-failure", **FAST)
        + service("c", sleep, enabled=True, restart="on-failure", **FAST)
    )
    box = running(text)
    wait_for(lambda: box.service("c")["phase"] == "ready" or None)
    for name in ("a", "b"):
        body = box.service(name)
        assert body["phase"] == "failed"
        assert "dependency cycle" in body["reason"]
        assert body["pid"] is None
    kinds = [row.get("kind") for row in disk_events(box.state) if row["event"] == "anomaly"]
    assert "dependency_cycle" in kinds


def test_dependency_waits_for_ready(running):
    sleep = py("import time", "time.sleep(60)")
    text = (
        service("first", sleep, enabled=True, restart="on-failure", **FAST)
        + service("second", sleep, depends_on=["first"], enabled=True, restart="on-failure", **FAST)
    )
    box = running(text)
    wait_for(lambda: box.service("second")["phase"] == "ready" or None)
    events = disk_events(box.state)
    ready = next(i for i, row in enumerate(events) if row["event"] == "ready" and row.get("service") == "first")
    started = next(i for i, row in enumerate(events) if row["event"] == "start" and row.get("service") == "second")
    assert ready < started


def test_missing_binary_degrades(running):
    sleep = py("import time", "time.sleep(30)")
    text = (
        service("gone", ["/no/such/avrone-svc-bin"], optional=True, enabled=True, restart="on-failure", **FAST)
        + service("needed", ["/no/such/avrone-svc-bin-required"], optional=False, enabled=True, restart="on-failure", **FAST)
        + service("ok", sleep, enabled=True, restart="on-failure", **FAST)
    )
    box = running(text)
    wait_for(lambda: box.service("gone")["phase"] == "unavailable" or None)
    wait_for(lambda: box.service("needed")["phase"] == "failed" or None)
    wait_for(lambda: box.service("ok")["phase"] == "ready" or None)
    assert "missing binary" in box.service("gone")["reason"]
    assert box.req("GET", "/v1/status", token=box.token)["ok"] is True
    anomalies = [row for row in disk_events(box.state) if row["event"] == "anomaly" and row.get("kind") == "missing_binary"]
    assert anomalies
    assert all(ISO_TS.fullmatch(row["ts"]) for row in anomalies)


def test_port_conflict_then_recovery(running):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    sleep = py("import time", "time.sleep(30)")
    box = running(service("held", sleep, ports=[port], enabled=True, restart="on-failure", **FAST))
    try:
        body = wait_for(lambda: box.service("held") if box.service("held")["phase"] == "port_conflict" else None)
        assert str(port) in body["reason"]
        assert body["pid"] is None
        conflict = [row for row in disk_events(box.state) if row.get("kind") == "port_conflict"]
        assert conflict and ISO_TS.fullmatch(conflict[0]["ts"])
    finally:
        sock.close()
    wait_for(lambda: box.service("held")["phase"] == "ready" or None)


def test_graceful_stop_then_sigkill(running, tmp_path):
    flag = tmp_path / "ignoring"
    script = (
        "import signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(flag)!r}).write_text('ignoring')\n"
        "time.sleep(60)\n"
    )
    box = running(service("stubborn", [sys.executable, "-c", script], restart="never", enabled=True, **{**FAST, "stop_grace_seconds": 0.4}))
    wait_for(lambda: flag.exists() or None, timeout=10)
    started = time.monotonic()
    box.req("POST", "/v1/services/stubborn/stop", token=box.token)
    body = wait_for(lambda: box.service("stubborn") if box.service("stubborn")["phase"] == "stopped" else None, timeout=10)
    assert time.monotonic() - started >= 0.3
    assert body["exit_code"] == -9


def test_oneshot_success_does_not_restart(running):
    box = running(service("job", py("import sys", "sys.exit(0)"), oneshot=True, restart="always", enabled=True, **FAST))
    wait_for(lambda: box.service("job")["phase"] == "finished" or None)
    starts = [row for row in disk_events(box.state) if row["event"] == "start" and row.get("service") == "job"]
    time.sleep(0.4)
    later = [row for row in disk_events(box.state) if row["event"] == "start" and row.get("service") == "job"]
    assert len(starts) == 1
    assert len(later) == 1


def test_unknown_user_fails_service_not_supervisor(running):
    box = running(service("who", py("import time", "time.sleep(30)"), user="no-such-avrone-user", enabled=True, restart="on-failure", **FAST))
    body = wait_for(lambda: box.service("who") if box.service("who")["phase"] == "failed" else None)
    assert "unknown user" in body["reason"]
    assert box.req("GET", "/v1/status", token=box.token)["ok"] is True


def test_rlimit_applied(running, tmp_path):
    out = tmp_path / "nofile"
    script = (
        "import os, resource, time\n"
        f"open({str(out)!r}, 'w').write(str(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))\n"
        "time.sleep(30)\n"
    )
    text = service("limited", [sys.executable, "-c", script], enabled=True, restart="on-failure", **FAST)
    text += f"\n[services.limited.limits]\nnofile = 256\n"
    box = running(text)
    wait_for(lambda: out.exists() or None, timeout=10)
    assert out.read_text(encoding="utf-8").strip() == "256"
    assert box.service("limited")["phase"] in {"starting", "ready", "running"}


def test_log_rotation_keeps_a_contiguous_tail(running):
    script = "import sys, time\nfor i in range(20000):\n    sys.stdout.write('%08d\\n' % i)\nsys.stdout.flush()\ntime.sleep(60)\n"
    box = running(service(
        "logger",
        [sys.executable, "-c", script],
        enabled=True,
        restart="on-failure",
        log_max_bytes=8192,
        log_backups=3,
        **{key: value for key, value in FAST.items() if key not in {"log_max_bytes", "log_backups"}},
    ))
    log_dir = box.state / "logs"

    def rotated():
        files = list(log_dir.glob("logger.log*"))
        if len(files) < 2:
            return None
        blob = _retained_log(log_dir, "logger")
        if b"00019999" not in blob:
            return None
        return blob

    blob = wait_for(rotated, timeout=20)
    numbers = [int(line) for line in blob.decode().splitlines() if line.strip().isdigit()]
    assert numbers[-1] == 19999
    assert numbers == list(range(numbers[0], numbers[-1] + 1))
    assert box.service("logger")["phase"] == "ready"


def _retained_log(log_dir: Path, name: str) -> bytes:
    chunks = []
    for index in range(3, 0, -1):
        path = log_dir / f"{name}.log.{index}"
        if path.exists():
            chunks.append(path.read_bytes())
    current = log_dir / f"{name}.log"
    if current.exists():
        chunks.append(current.read_bytes())
    return b"".join(chunks)


def test_many_services_at_once(running):
    sleep = py("import time", "time.sleep(60)")
    parts = [service(f"s{i}", sleep, enabled=True, restart="on-failure", **FAST) for i in range(24)]
    box = running("".join(parts))
    def ready_count():
        rows = box.req("GET", "/v1/services", token=box.token)["services"]
        return rows if rows and all(row["phase"] == "ready" for row in rows) else None
    rows = wait_for(ready_count, timeout=20)
    assert len(rows) == 24


def test_api_requires_token_and_loopback(running):
    from avrone.svcman.supervisor import Supervisor, SupervisorError
    with pytest.raises(SupervisorError):
        Supervisor(Path("."), Path("unused-state"), api_host="0.0.0.0")
    box = running(service("idle", py("import time", "time.sleep(30)"), enabled=True, restart="on-failure", **FAST))
    request = urllib.request.Request(box.base + "/v1/status")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as unauth:
        opener.open(request, timeout=5)
    assert unauth.value.code == 401
    request = urllib.request.Request(box.base + "/v1/status")
    request.add_header("Authorization", "Bearer nope")
    with pytest.raises(urllib.error.HTTPError) as wrong:
        opener.open(request, timeout=5)
    assert wrong.value.code == 401
    body = box.req("GET", "/v1/status", token=box.token)
    assert body["ok"] is True
    assert body["api"].startswith("http://127.0.0.1:")


def test_enable_disable_persists(tmp_path):
    text = service("gated", py("import time", "time.sleep(60)"), enabled=False, restart="on-failure", **FAST)
    first = Running(tmp_path, text)
    try:
        assert first.service("gated")["phase"] == "stopped"
        first.req("POST", "/v1/services/gated/enable", token=first.token)
        wait_for(lambda: first.service("gated")["phase"] == "ready" or None)
        first.sup.request_shutdown(mark_stopped=False)
        first.thread.join(10)
    finally:
        if first.thread.is_alive():
            first.close()
    second = Running(tmp_path, text)
    try:
        wait_for(lambda: second.service("gated")["phase"] == "ready" or None)
        second.req("POST", "/v1/services/gated/disable", token=second.token)
        wait_for(lambda: second.service("gated")["phase"] == "stopped" or None)
        second.sup.request_shutdown(mark_stopped=False)
        second.thread.join(10)
    finally:
        if second.thread.is_alive():
            second.close()
    third = Running(tmp_path, text)
    try:
        time.sleep(0.5)
        assert third.service("gated")["desired"] == "stopped"
        assert third.service("gated")["enabled"] is False
    finally:
        third.close()


def test_yaml_and_toml_agree(tmp_path):
    from avrone.svcman.spec import load_file
    toml = tmp_path / "a.toml"
    yaml = tmp_path / "a.yaml"
    toml.write_text(
        '[services.demo]\ncommand = ["sleep", "1"]\nrestart = "on-failure"\nports = [9]\n\n'
        '[services.demo.env]\nFOO = "bar"\n\n[services.demo.health]\ntype = "process"\n',
        encoding="utf-8",
    )
    yaml.write_text(
        "services:\n  demo:\n    command: [\"sleep\", \"1\"]\n    restart: on-failure\n    ports: [9]\n"
        "    env:\n      FOO: bar\n    health:\n      type: process\n",
        encoding="utf-8",
    )
    left = load_file(toml)[0]
    right = load_file(yaml)[0]
    assert left.command == right.command == ["sleep", "1"]
    assert left.restart == right.restart == "on-failure"
    assert left.ports == right.ports == [9]
    assert left.env["FOO"] == right.env["FOO"] == "bar"
    assert left.health is not None and right.health is not None
    assert left.health.type == right.health.type == "process"


def test_units_are_optional_text(tmp_path):
    from avrone.svcman.spec import load_config
    from avrone.svcman.units import write_units
    specs = load_config(ROOT / "avrone" / "svcman" / "bundle")
    written = write_units(specs, "systemd", tmp_path / "systemd")
    launchd = write_units(specs, "launchd", tmp_path / "launchd")
    unit = (tmp_path / "systemd" / "avrone-ollama.service").read_text(encoding="utf-8")
    assert "does not require systemd or launchd" in unit
    assert "ExecStart=" in unit
    assert "ollama" in unit
    plist = (tmp_path / "launchd" / "dev.avrone.ollama.plist").read_text(encoding="utf-8")
    assert "does not require systemd or launchd" in plist
    assert "<key>ProgramArguments</key>" in plist
    assert written and launchd


def test_bundle_shape():
    from avrone.svcman.spec import load_config
    specs = {spec.name: spec for spec in load_config(ROOT / "avrone" / "svcman" / "bundle")}
    assert specs["ollama"].optional is True
    assert specs["ollama"].ports == [11434]
    assert specs["ollama"].health is not None and specs["ollama"].health.url.endswith("/api/version")
    assert specs["avrone"].ports == [8710]
    assert specs["avrone"].optional is False
    for name in ("lane-deepcov", "lane-newmods", "lane-tools", "lane-robust"):
        assert specs[name].enabled is False
        assert specs[name].oneshot is True
        assert specs[name].depends_on == ["ollama"]
        assert specs[name].restart == "never"
    assert specs["cyclekernel"].ports == [8090]
    assert specs["cyclekernel"].optional is True
    assert specs["capability-authority"].ports == [8095]
    assert specs["fusion-bridge"].optional is True
    assert specs["avrone-daemon"].optional is True


def test_cgroup_writer_on_a_fake_tree(tmp_path):
    from avrone.svcman.cgroup import apply
    from avrone.svcman.spec import LimitSpec
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("memory cpu pids\n", encoding="utf-8")
    (root / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (root / "cgroup.procs").write_text("", encoding="utf-8")
    ok, detail = apply("demo", os.getpid(), LimitSpec(memory_max="10000000", pids_max="32"), root=root)
    assert ok, detail
    group = root / "avrone-svc" / "demo"
    assert group.joinpath("memory.max").read_text(encoding="utf-8").strip() == "10000000"
    assert group.joinpath("pids.max").read_text(encoding="utf-8").strip() == "32"
    assert str(os.getpid()) in group.joinpath("cgroup.procs").read_text(encoding="utf-8")


def test_cli_status_and_check(tmp_path):
    text = service("idle", py("import time", "time.sleep(30)"), enabled=True, restart="on-failure", **FAST)
    proc, state = spawn_cli(tmp_path, text)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "avrone.svcman", "status", "--state-dir", str(state)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "idle" in completed.stdout
        checked = subprocess.run(
            [sys.executable, "-m", "avrone.svcman", "check", "--config", str(ROOT / "avrone" / "svcman" / "bundle")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "ollama" in checked.stdout
        assert "unavailable" in checked.stdout or "ready-to-start" in checked.stdout
    finally:
        stop_cli(proc, state)


def test_signal_storm_and_sighup_reload(tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "base.toml").write_text(
        service("sleeper", py("import time", "time.sleep(60)"), enabled=True, restart="always", **FAST),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    log = open(tmp_path / "supervisor.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "avrone.svcman", "run", "--config", str(config), "--state-dir", str(state), "--api-port", "0", "--tick", "0.05"],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (state / "api.json").exists():
            if proc.poll() is not None:
                raise AssertionError(log_text(tmp_path / "supervisor.log"))
            time.sleep(0.05)
        from avrone.svcman.client import request
        def ready():
            try:
                body = request(state, "GET", "/v1/services/sleeper", timeout=2)
            except Exception:
                return None
            return body if body.get("phase") == "ready" else None
        wait_for(ready, timeout=15)
        for _ in range(200):
            os.kill(proc.pid, signal.SIGHUP)
        for _ in range(40):
            os.kill(proc.pid, signal.SIGCHLD)
        assert proc.poll() is None
        assert request(state, "GET", "/v1/status", timeout=3)["ok"] is True
        (config / "extra.toml").write_text(
            service("extra", py("import time", "time.sleep(60)"), enabled=True, restart="on-failure", **FAST),
            encoding="utf-8",
        )
        os.kill(proc.pid, signal.SIGHUP)
        def extra_ready():
            try:
                body = request(state, "GET", "/v1/services/extra", timeout=2)
            except Exception:
                return None
            return body if body.get("phase") == "ready" else None
        wait_for(extra_ready, timeout=15)
        assert proc.poll() is None
    finally:
        stop_cli(proc, state)


def test_supervisor_kill9_resumes_and_restarts_service(tmp_path):
    text = service("sleeper", py("import time", "time.sleep(120)"), enabled=True, restart="always", **FAST)
    proc, state = spawn_cli(tmp_path, text)
    from avrone.svcman.client import request
    try:
        def ready_pid():
            try:
                body = request(state, "GET", "/v1/services/sleeper", timeout=2)
            except Exception:
                return None
            return body["pid"] if body.get("phase") == "ready" and body.get("pid") else None
        pid = wait_for(ready_pid, timeout=15)
        assert _alive(pid)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(5)
        assert _alive(pid)
        proc2, state2 = spawn_cli(tmp_path, text)
        assert state2 == state
        try:
            def adopted():
                try:
                    body = request(state, "GET", "/v1/services/sleeper", timeout=2)
                except Exception:
                    return None
                return body if body.get("pid") == pid and body.get("phase") == "ready" and body.get("adopted") else None
            wait_for(adopted, timeout=15)
            os.kill(pid, signal.SIGKILL)
            def replaced():
                try:
                    body = request(state, "GET", "/v1/services/sleeper", timeout=2)
                except Exception:
                    return None
                return body if body.get("pid") and body["pid"] != pid and body.get("phase") == "ready" else None
            new_body = wait_for(replaced, timeout=15)
            assert new_body["restarts"] >= 1
            events = disk_events(state)
            assert any(row["event"] in {"crash", "restart"} and row.get("service") == "sleeper" for row in events)
            assert all(ISO_TS.fullmatch(row["ts"]) for row in events)
        finally:
            stop_cli(proc2, state)
    finally:
        if proc.poll() is None:
            stop_cli(proc, state)


def test_zombie_reaping_as_pid1(tmp_path):
    if not _unshare_works():
        pytest.skip("sudo unshare --pid is not available")
    script = tmp_path / "pid1_probe.py"
    script.write_text(PID1_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [
            "sudo", "-n", "env", f"PYTHONPATH={ROOT}",
            "unshare", "--pid", "--fork", "--mount-proc",
            sys.executable, str(script), str(tmp_path / "probe-state"), str(ROOT),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "ORPHANS" in completed.stdout
    assert "SAW3 1" in completed.stdout


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _unshare_works() -> bool:
    completed = subprocess.run(
        ["sudo", "-n", "unshare", "--pid", "--fork", "--mount-proc", sys.executable, "-c", "import os; raise SystemExit(0 if os.getpid()==1 else 1)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


PID1_PROBE = r'''
import os, sys, time
from pathlib import Path
state = Path(sys.argv[1])
root = Path(sys.argv[2])
os.chdir(root)
from avrone.svcman.supervisor import Supervisor

script = (
    "import os, time\n"
    "pid = os.fork()\n"
    "if pid > 0:\n"
    "    os._exit(0)\n"
    "g = os.fork()\n"
    "if g > 0:\n"
    "    os._exit(0)\n"
    "time.sleep(0.4)\n"
    "os._exit(3)\n"
)
import json
config = state.parent / "pid1.toml"
config.write_text(
    "[services.reaper]\n"
    "command = " + json.dumps([sys.executable, "-c", script]) + "\n"
    "enabled = true\n"
    "restart = \"on-failure\"\n"
    "oneshot = false\n"
    "stop_grace_seconds = 0.3\n"
    "backoff_initial_seconds = 0.05\n"
    "backoff_max_seconds = 0.2\n"
    "backoff_multiplier = 2\n"
    "jitter = 0\n"
    "max_restarts = 5\n"
    "restart_window_seconds = 30\n"
    "circuit_reset_seconds = 30\n"
    "stable_seconds = 30\n",
    encoding="utf-8",
)
sup = Supervisor(config, state, api_port=0, tick=0.05, handle_signals=True, subreaper=True, repo=root)
import threading
box = {"saw": False}
def watch():
    if not sup.wait_ready(10):
        sup.request_shutdown(mark_stopped=True)
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        events = sup.events.tail(500)
        codes = [row.get("exit_code") for row in events if row.get("event") == "orphan_reaped"]
        if sup.reaped_orphans >= 2 and 3 in codes:
            box["saw"] = True
            break
        time.sleep(0.05)
    sup.request_shutdown(mark_stopped=True)
threading.Thread(target=watch, daemon=True).start()
sup.run()
print(f"ORPHANS {sup.reaped_orphans}")
print(f"SAW3 {1 if box['saw'] else 0}")
print(f"PID {os.getpid()}")
sys.exit(0 if box["saw"] else 1)
'''
