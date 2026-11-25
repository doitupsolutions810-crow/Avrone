"""Service supervisor loop: dependencies, health, restarts, reaping, reload.

The loop is the only thread that spawns and reaps. The control API mutates
desired state under the same lock and wakes the loop. Running as pid 1, or as
a child subreaper under tini, the loop reaps adopted orphans with waitpid(-1)
so zombies do not accumulate.
"""

from __future__ import annotations

import os
import secrets
import selectors
import shlex
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import cgroup, health, process
from .logs import EventLog, RotatingLog
from .ports import first_conflict
from .spec import ServiceSpec, find_cycles, load_config
from .state import InstanceLock, StateError, StateStore
from .timeutil import iso_now

LOOPBACK = {"127.0.0.1", "::1"}


class SupervisorError(RuntimeError):
    pass


@dataclass
class Runtime:
    spec: ServiceSpec
    desired: str = "stopped"
    phase: str = "stopped"
    reason: str = ""
    pid: int | None = None
    nonce: str = ""
    child: process.Child | None = None
    reader: threading.Thread | None = None
    log: RotatingLog | None = None
    tail: bytearray = field(default_factory=bytearray)
    tail_lock: threading.Lock = field(default_factory=threading.Lock)
    adopted: bool = False
    intentional: bool = False
    restart_after: bool = False
    half_open: bool = False
    removed: bool = False
    enabled_override: bool | None = None
    started_mono: float = 0.0
    started_at: str = ""
    ready_since: float = 0.0
    exit_code: int | None = None
    attempt: int = 0
    restarts: int = 0
    restart_epochs: list[float] = field(default_factory=list)
    backoff_until: float = 0.0
    circuit_until: float = 0.0
    circuit_until_epoch: float = 0.0
    stop_deadline: float = 0.0
    next_health: float = 0.0
    health_ok: int = 0
    health_bad: int = 0
    spawn_claimed: bool = False

    @property
    def enabled(self) -> bool:
        if self.enabled_override is None:
            return self.spec.enabled
        return bool(self.enabled_override)

    def trace_text(self) -> str:
        with self.tail_lock:
            return bytes(self.tail).decode("utf-8", "replace")

    def plan(self, now: float) -> str:
        if self.removed and not self.pid:
            return "drop"
        if self.phase == "stopping":
            return "stop_check"
        if self.pid:
            if self.spec.runtime_limit_seconds and now - self.started_mono > self.spec.runtime_limit_seconds:
                return "runtime_kill"
            if self.phase == "starting" and (self.spec.health is None or self.spec.health.type == "process"):
                return "mark_ready"
            health_spec = self.spec.health
            if (
                self.phase == "starting"
                and health_spec is not None
                and now - self.started_mono > self.spec.start_timeout_seconds
                and self.health_ok < health_spec.healthy_threshold
            ):
                return "start_timeout"
            if health_spec is not None and health_spec.type != "process" and now >= self.next_health:
                return "health"
            if self.phase == "ready" and self.restart_epochs and now - self.ready_since > self.spec.stable_seconds:
                return "clear_backoff"
            return "idle"
        if self.desired != "running":
            return "idle"
        if "dependency cycle" in self.reason:
            return "idle"
        if self.phase == "circuit_open":
            return "idle" if now < self.circuit_until else "half_open"
        if self.phase == "backoff" and now < self.backoff_until:
            return "idle"
        if self.phase == "finished":
            return "idle"
        if self.phase == "failed" and (self.spec.restart == "never" or now < self.backoff_until):
            return "idle"
        if self.phase in {"unavailable", "port_conflict"} and now < self.backoff_until:
            return "idle"
        return "spawn"

    def snapshot(self) -> dict:
        return {
            "name": self.spec.name,
            "description": self.spec.description,
            "phase": self.phase,
            "desired": self.desired,
            "enabled": self.enabled,
            "optional": self.spec.optional,
            "pid": self.pid,
            "adopted": self.adopted,
            "restarts": self.restarts,
            "attempt": self.attempt,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "started_at": self.started_at,
            "command": list(self.spec.command),
            "depends_on": list(self.spec.depends_on),
        }


class Supervisor:
    def __init__(
        self,
        config: Path,
        state_dir: Path,
        api_host: str = "127.0.0.1",
        api_port: int = 8799,
        tick: float = 0.2,
        handle_signals: bool = False,
        subreaper: bool = False,
        repo: Path | None = None,
    ):
        if api_host not in LOOPBACK:
            raise SupervisorError(f"control API must bind to loopback, not {api_host}")
        self.config = Path(config)
        self.state_dir = Path(state_dir)
        self.api_host = api_host
        self.api_port = int(api_port)
        self.tick = float(tick)
        self.handle_signals = handle_signals
        self.subreaper = subreaper or os.getpid() == 1
        self.repo = Path(repo) if repo else find_repo(self.config)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mapping = {
            "python": sys.executable,
            "repo": str(self.repo),
            "state": str(self.state_dir),
        }
        self.runtime: dict[str, Runtime] = {}
        self._by_pid: dict[int, Runtime] = {}
        self._tracked: set[int] = set()
        self._lock = threading.RLock()
        self._stop = False
        self._mark_stopped = False
        self._reload_flag = False
        self._prepared = False
        self.reap_all = False
        self.reaped_total = 0
        self.reaped_orphans = 0
        self.started_at = ""
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self._wake_r = read_fd
        self._wake_w = write_fd
        self._selector = selectors.DefaultSelector()
        self._selector.register(read_fd, selectors.EVENT_READ)
        self.ready = threading.Event()
        self.events = EventLog(self.state_dir / "events.jsonl")
        self._state = StateStore(self.state_dir / "state.json")
        self._instance: InstanceLock | None = None
        self._api = None
        self._api_thread: threading.Thread | None = None
        self._token = ""

    def run(self) -> int:
        try:
            self._prepare()
            while not self._stop:
                self._drain()
                if self._reload_flag:
                    self._reload_flag = False
                    try:
                        self.reload()
                    except SupervisorError:
                        pass
                self._reap()
                self._tick()
                if not self._stop:
                    self._sleep()
            return 0
        except (SupervisorError, StateError) as exc:
            self.events.emit("anomaly", kind="supervisor", detail=str(exc), trace=traceback.format_exc())
            sys.stderr.write(f"avrone-svc: {exc}\n")
            return 1
        finally:
            self._shutdown()

    def request_shutdown(self, mark_stopped: bool = False) -> None:
        self._mark_stopped = mark_stopped or self._mark_stopped
        self._stop = True
        self._poke()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self.ready.wait(timeout)

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "pid": os.getpid(),
                "state_dir": str(self.state_dir),
                "config": str(self.config),
                "repo": str(self.repo),
                "api": f"http://{self.api_host}:{self.api_port}",
                "started_at": self.started_at,
                "reaped_total": self.reaped_total,
                "reaped_orphans": self.reaped_orphans,
                "subreaper": self.reap_all,
                "services": [rt.snapshot() for rt in self.runtime.values()],
            }

    def service_status(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            return rt.snapshot()

    def logs(self, name: str, lines: int = 200) -> dict:
        with self._lock:
            rt = self._require(name)
            log = rt.log
        if log is None:
            return {"name": name, "lines": []}
        return {"name": name, "lines": log.tail_lines(lines)}

    def events_tail(self, lines: int = 200) -> dict:
        return {"events": self.events.tail(lines)}

    def start_service(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            if "dependency cycle" in rt.reason:
                raise SupervisorError(rt.reason)
            rt.desired = "running"
            rt.removed = False
            rt.half_open = False
            rt.circuit_until = 0.0
            rt.backoff_until = 0.0
            if rt.phase in {"finished", "failed", "unavailable", "port_conflict", "circuit_open", "backoff"}:
                rt.phase = "stopped"
                rt.reason = ""
            self._persist_locked(rt)
        self._poke()
        return self.service_status(name)

    def stop_service(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            rt.desired = "stopped"
            rt.restart_after = False
            self._begin_stop(rt, intentional=True)
            self._persist_locked(rt)
        self._poke()
        return self.service_status(name)

    def restart_service(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            if "dependency cycle" in rt.reason:
                raise SupervisorError(rt.reason)
            rt.desired = "running"
            rt.half_open = False
            rt.circuit_until = 0.0
            rt.backoff_until = 0.0
            rt.restart_after = True
            if rt.pid:
                self._begin_stop(rt, intentional=True)
            else:
                rt.phase = "stopped"
                rt.reason = ""
            self._persist_locked(rt)
        self._poke()
        return self.service_status(name)

    def enable_service(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            rt.enabled_override = True
            self._persist_locked(rt)
        return self.start_service(name)

    def disable_service(self, name: str) -> dict:
        with self._lock:
            rt = self._require(name)
            rt.enabled_override = False
            rt.desired = "stopped"
            rt.restart_after = False
            self._begin_stop(rt, intentional=True)
            self._persist_locked(rt)
        self._poke()
        return self.service_status(name)

    def start_enabled(self) -> dict:
        with self._lock:
            names = [rt.spec.name for rt in self.runtime.values() if rt.enabled and "dependency cycle" not in rt.reason]
        for name in names:
            self.start_service(name)
        return self.status()

    def stop_all(self) -> dict:
        with self._lock:
            names = list(self.runtime)
        for name in names:
            self.stop_service(name)
        return self.status()

    def reload(self) -> dict:
        with self._lock:
            try:
                specs = [spec.substitute(self.mapping) for spec in load_config(self.config)]
            except Exception as exc:
                self.events.emit("anomaly", kind="reload_failed", detail=str(exc), trace=traceback.format_exc())
                raise SupervisorError(str(exc)) from exc
            incoming = {spec.name: spec for spec in specs}
            for name, rt in list(self.runtime.items()):
                if name not in incoming:
                    rt.removed = True
                    rt.desired = "stopped"
                    rt.restart_after = False
                    self._begin_stop(rt, intentional=True)
            for spec in specs:
                current = self.runtime.get(spec.name)
                if current is None or current.removed:
                    runtime = Runtime(spec=spec)
                    runtime.log = self._open_log(runtime)
                    self._restore(runtime)
                    self.runtime[spec.name] = runtime
                    continue
                changed = (
                    current.spec.command != spec.command
                    or current.spec.env != spec.env
                    or current.spec.cwd != spec.cwd
                )
                current.spec = spec
                current.removed = False
                if changed and current.pid:
                    current.restart_after = True
                    self._begin_stop(current, intentional=True)
            self._mark_cycles()
            self.events.emit("reload", detail=f"{len(specs)} services")
        self._poke()
        return self.status()

    def _prepare(self) -> None:
        self._instance = InstanceLock(self.state_dir / "supervisor.lock")
        (self.state_dir / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
        self._token = load_or_create_token(self.state_dir / "token")
        specs = [spec.substitute(self.mapping) for spec in load_config(self.config)]
        for spec in specs:
            runtime = Runtime(spec=spec)
            runtime.log = self._open_log(runtime)
            self.runtime[spec.name] = runtime
        self._mark_cycles()
        self._cleanup_removed()
        for runtime in self.runtime.values():
            self._restore(runtime)
        if self.subreaper:
            process.enable_subreaper()
            self.reap_all = True
        if os.getpid() == 1:
            self.reap_all = True
        if self.handle_signals:
            self._install_signals()
        self._start_api()
        self.started_at = iso_now()
        self.events.emit(
            "supervisor_start",
            pid=os.getpid(),
            detail=f"api http://{self.api_host}:{self.api_port}",
            subreaper=self.reap_all,
        )
        self._prepared = True
        self.ready.set()

    def _shutdown(self) -> None:
        try:
            if self._prepared:
                self._shutdown_children()
                self.events.emit("supervisor_stop", pid=os.getpid(), detail="mark_stopped" if self._mark_stopped else "resume")
        except Exception:
            traceback.print_exc()
        if self._api is not None:
            try:
                self._api.shutdown()
            except Exception:
                pass
            try:
                self._api.server_close()
            except Exception:
                pass
        for runtime in self.runtime.values():
            if runtime.log is not None:
                runtime.log.close()
        try:
            self.events.close()
        except Exception:
            pass
        if self._instance is not None:
            self._instance.release()
            self._instance = None
        self.ready.set()

    def _shutdown_children(self) -> None:
        with self._lock:
            for runtime in self.runtime.values():
                if self._mark_stopped:
                    runtime.desired = "stopped"
                if runtime.pid:
                    runtime.restart_after = False
                    self._begin_stop(runtime, intentional=True)
                self._persist_locked(runtime)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            self._reap()
            hanging = False
            with self._lock:
                now = time.monotonic()
                for runtime in self.runtime.values():
                    if runtime.pid and process.alive(runtime.pid):
                        hanging = True
                        if now >= runtime.stop_deadline:
                            process.signal_group(runtime.pid, signal.SIGKILL)
                            runtime.stop_deadline = now + 0.5
            if not hanging:
                break
            time.sleep(0.05)
        self._reap()

    def _tick(self) -> None:
        with self._lock:
            planned = [(name, runtime.plan(time.monotonic())) for name, runtime in self.runtime.items()]
        for name, action in planned:
            try:
                self._dispatch(name, action)
            except Exception:
                self.events.emit(
                    "anomaly",
                    kind="tick",
                    service=name,
                    detail="service tick failed",
                    trace=traceback.format_exc(),
                )

    def _dispatch(self, name: str, action: str) -> None:
        runtime = self.runtime.get(name)
        if runtime is None or action == "idle":
            return
        now = time.monotonic()
        if action == "drop":
            with self._lock:
                if runtime.removed and not runtime.pid:
                    self.runtime.pop(name, None)
            return
        if action == "stop_check":
            with self._lock:
                if runtime.pid and process.alive(runtime.pid) and now >= runtime.stop_deadline:
                    process.signal_group(runtime.pid, signal.SIGKILL)
                    runtime.stop_deadline = now + 0.5
            return
        if action == "mark_ready":
            with self._lock:
                if runtime.pid and runtime.phase in {"starting", "running"}:
                    self._become_ready(runtime, "process running")
            return
        if action == "clear_backoff":
            with self._lock:
                runtime.restart_epochs.clear()
                runtime.attempt = 0
                self._persist_locked(runtime)
            return
        if action in {"runtime_kill", "start_timeout"}:
            with self._lock:
                self._kill_for_restart(runtime, action)
            return
        if action == "health":
            self._do_health(runtime)
            return
        if action == "half_open":
            with self._lock:
                runtime.half_open = True
                runtime.phase = "stopped"
                self.events.emit("circuit_half_open", service=runtime.spec.name)
            self._spawn(runtime)
            return
        if action == "spawn":
            self._spawn(runtime)

    def _spawn(self, runtime: Runtime) -> None:
        with self._lock:
            if runtime.desired != "running" or runtime.pid or runtime.spawn_claimed:
                return
            if "dependency cycle" in runtime.reason:
                return
            gate = self._dependency_gate(runtime)
            if gate != "ok":
                return
            command = self._command(runtime)
            binary = command[0]
            if not process.binary_exists(binary):
                self._mark_missing(runtime, binary)
                return
            if runtime.spec.user:
                ok, detail = process.can_switch_user(runtime.spec.user)
                if not ok:
                    self._mark_phase(runtime, "failed", detail, "user")
                    runtime.backoff_until = time.monotonic() + 5.0
                    return
            conflict = first_conflict(runtime.spec.ports)
            if conflict is not None:
                self._mark_phase(runtime, "port_conflict", f"port {conflict} is already in use", "port_conflict")
                runtime.backoff_until = time.monotonic() + 1.0
                return
            runtime.spawn_claimed = True
            runtime.phase = "starting"
            nonce = secrets.token_hex(8)
            runtime.nonce = nonce
            limits = runtime.spec.limits
            user = runtime.spec.user
            cwd = runtime.spec.cwd
            env = self._child_env(runtime, nonce)
        try:
            child = process.spawn(command, cwd, env, user, limits)
        except Exception as exc:
            with self._lock:
                runtime.spawn_claimed = False
                runtime.backoff_until = time.monotonic() + 1.0
                self._mark_phase(runtime, "failed", f"spawn failed: {exc}", "spawn_failed")
                self.events.emit("anomaly", service=runtime.spec.name, kind="spawn_failed", detail=str(exc), trace=traceback.format_exc())
            return
        with self._lock:
            runtime.spawn_claimed = False
            if runtime.desired != "running":
                process.signal_group(child.pid, signal.SIGTERM)
                runtime.phase = "stopped"
                return
            runtime.child = child
            runtime.pid = child.pid
            runtime.adopted = False
            runtime.started_mono = time.monotonic()
            runtime.started_at = iso_now()
            runtime.phase = "starting"
            runtime.health_ok = 0
            runtime.health_bad = 0
            runtime.next_health = time.monotonic()
            runtime.exit_code = None
            runtime.reason = ""
            with runtime.tail_lock:
                runtime.tail.clear()
            self._by_pid[child.pid] = runtime
            self._tracked.add(child.pid)
            self.events.emit("start", service=runtime.spec.name, pid=child.pid, command=command)
            self._persist_locked(runtime)
        self._start_reader(runtime, child)
        if limits is not None and limits.has_cgroup():
            try:
                applied, detail = cgroup.apply(runtime.spec.name, child.pid, limits)
            except OSError as exc:
                applied, detail = False, str(exc)
                with self._lock:
                    self._kill_for_restart(runtime, "cgroup_required")
            if not applied:
                self.events.emit("anomaly", service=runtime.spec.name, kind="cgroup_skipped", detail=detail)

    def _do_health(self, runtime: Runtime) -> None:
        with self._lock:
            spec = runtime.spec.health
            if not runtime.pid or spec is None:
                return
            runtime.next_health = time.monotonic() + spec.interval_seconds
        try:
            ok, detail = health.check(spec)
        except Exception as exc:
            ok, detail = False, str(exc)
        with self._lock:
            if not runtime.pid:
                return
            if ok:
                runtime.health_bad = 0
                runtime.health_ok += 1
                if runtime.health_ok >= spec.healthy_threshold and runtime.phase != "ready":
                    self._become_ready(runtime, detail)
            else:
                runtime.health_ok = 0
                runtime.health_bad += 1
                self.events.emit("health_fail", service=runtime.spec.name, detail=detail, streak=runtime.health_bad)
                if runtime.health_bad >= spec.unhealthy_threshold:
                    self.events.emit("anomaly", service=runtime.spec.name, kind="unhealthy", detail=detail)
                    if runtime.spec.restart == "never":
                        runtime.phase = "unhealthy"
                        runtime.reason = detail
                        self._persist_locked(runtime)
                    else:
                        self._kill_for_restart(runtime, "unhealthy")

    def _reap(self) -> None:
        if self.reap_all:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                self._note_wait(pid, status)
        else:
            for pid in list(self._tracked):
                try:
                    got, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    continue
                if got:
                    self._note_wait(got, status)
        self._poll_adopted()

    def _poll_adopted(self) -> None:
        """Adopted pids are not our children after a supervisor kill -9, so waitpid cannot see them."""
        for runtime in list(self.runtime.values()):
            pid = runtime.pid
            if not runtime.adopted or not pid or process.alive(pid):
                continue
            with self._lock:
                if runtime.pid != pid:
                    continue
                self._by_pid.pop(pid, None)
                self._tracked.discard(pid)
                runtime.pid = None
                runtime.adopted = False
                self._finish_exit(runtime, None)

    def _note_wait(self, pid: int, status: int) -> None:
        code = process.decode_status(status)
        with self._lock:
            self.reaped_total += 1
            runtime = self._by_pid.pop(pid, None)
            self._tracked.discard(pid)
            if runtime is None:
                self.reaped_orphans += 1
                self.events.emit("orphan_reaped", pid=pid, exit_code=code)
                return
            if runtime.child is not None and runtime.child.popen is not None:
                runtime.child.popen.returncode = code
            reader = runtime.reader
            if runtime.pid == pid:
                runtime.pid = None
        if reader is not None:
            reader.join(timeout=1.0)
        with self._lock:
            self._finish_exit(runtime, code)

    def _finish_exit(self, runtime: Runtime, code: int | None) -> None:
        trace = runtime.trace_text()
        disappeared = code is None
        runtime.exit_code = code
        runtime.pid = None
        runtime.child = None
        intentional = runtime.intentional
        restart_after = runtime.restart_after
        runtime.intentional = False
        runtime.restart_after = False
        crashed = (not intentional) and (disappeared or code != 0)
        self.events.emit(
            "crash" if crashed else "stop",
            service=runtime.spec.name,
            exit_code="unknown" if disappeared else code,
            trace=trace if crashed or "Traceback" in trace else "",
            intentional=intentional,
            detail="adopted process disappeared" if disappeared else None,
        )
        if runtime.desired != "running" and not restart_after:
            runtime.phase = "stopped"
            self._persist_locked(runtime)
            return
        if intentional and restart_after:
            runtime.phase = "stopped"
            self._persist_locked(runtime)
            return
        if runtime.spec.oneshot and code == 0 and not disappeared:
            runtime.phase = "finished"
            runtime.desired = "stopped"
            runtime.reason = "oneshot completed"
            self._persist_locked(runtime)
            return
        if runtime.spec.restart == "never" or (runtime.spec.restart == "on-failure" and code == 0 and not disappeared):
            runtime.phase = "failed" if code != 0 else "finished"
            runtime.reason = f"exit {code}"
            self._persist_locked(runtime)
            return
        self._schedule_restart(runtime, -1 if disappeared else code, trace)

    def _schedule_restart(self, runtime: Runtime, code: int, trace: str) -> None:
        now = time.monotonic()
        wall = time.time()
        if runtime.half_open:
            runtime.half_open = False
            runtime.phase = "circuit_open"
            runtime.circuit_until = now + runtime.spec.circuit_reset_seconds
            runtime.circuit_until_epoch = wall + runtime.spec.circuit_reset_seconds
            runtime.reason = "circuit open after half-open failure"
            self.events.emit(
                "circuit_open", service=runtime.spec.name, exit_code=code, trace=trace, detail=runtime.reason,
            )
            self._persist_locked(runtime)
            return
        window = runtime.spec.restart_window_seconds
        runtime.restart_epochs = [stamp for stamp in runtime.restart_epochs if wall - stamp <= window]
        if len(runtime.restart_epochs) >= runtime.spec.max_restarts:
            runtime.phase = "circuit_open"
            runtime.circuit_until = now + runtime.spec.circuit_reset_seconds
            runtime.circuit_until_epoch = wall + runtime.spec.circuit_reset_seconds
            runtime.reason = f"circuit open after {len(runtime.restart_epochs)} restarts in {window:.0f}s"
            self.events.emit(
                "circuit_open",
                service=runtime.spec.name,
                exit_code=code,
                trace=trace,
                detail=runtime.reason,
                restarts=len(runtime.restart_epochs),
            )
            self._persist_locked(runtime)
            return
        delay = runtime.spec.delay(runtime.attempt)
        runtime.attempt += 1
        runtime.restarts += 1
        runtime.restart_epochs.append(wall)
        runtime.phase = "backoff"
        runtime.backoff_until = now + delay
        runtime.reason = f"restart in {delay:.3f}s after exit {code}"
        self.events.emit(
            "restart",
            service=runtime.spec.name,
            exit_code=code,
            trace=trace,
            delay_seconds=round(delay, 3),
            attempt=runtime.attempt,
        )
        self._persist_locked(runtime)

    def _dependency_gate(self, runtime: Runtime) -> str:
        for name in runtime.spec.depends_on:
            dep = self.runtime.get(name)
            if dep is None:
                runtime.phase = "blocked"
                runtime.reason = f"unknown dependency {name}"
                return "wait"
            if dep.phase == "ready" or (dep.spec.oneshot and dep.phase == "finished"):
                continue
            if dep.phase == "unavailable" or (dep.phase == "failed" and dep.spec.optional):
                self._mark_phase(runtime, "unavailable", f"dependency {name} unavailable", "degraded")
                runtime.backoff_until = time.monotonic() + 2.0
                return "degrade"
            runtime.phase = "blocked"
            runtime.reason = f"waiting for {name} ({dep.phase})"
            return "wait"
        return "ok"

    def _mark_missing(self, runtime: Runtime, binary: str) -> None:
        phase = "unavailable" if runtime.spec.optional else "failed"
        self._mark_phase(runtime, phase, f"missing binary: {binary}", "missing_binary")
        runtime.backoff_until = time.monotonic() + 2.0
        runtime.spawn_claimed = False

    def _mark_phase(self, runtime: Runtime, phase: str, reason: str, kind: str) -> None:
        changed = runtime.phase != phase or runtime.reason != reason
        runtime.phase = phase
        runtime.reason = reason
        runtime.spawn_claimed = False
        if changed:
            self.events.emit("anomaly", service=runtime.spec.name, kind=kind, detail=reason, optional=runtime.spec.optional)
            self._persist_locked(runtime)

    def _become_ready(self, runtime: Runtime, detail: str) -> None:
        runtime.phase = "ready"
        runtime.ready_since = time.monotonic()
        runtime.reason = detail
        self.events.emit("ready", service=runtime.spec.name, pid=runtime.pid, detail=detail)
        self._persist_locked(runtime)

    def _kill_for_restart(self, runtime: Runtime, reason: str) -> None:
        runtime.reason = reason
        runtime.intentional = False
        if runtime.pid:
            process.signal_group(runtime.pid, signal.SIGTERM)
            runtime.phase = "stopping"
            runtime.stop_deadline = time.monotonic() + runtime.spec.stop_grace_seconds
        self.events.emit("anomaly", service=runtime.spec.name, kind=reason, detail=reason)

    def _begin_stop(self, runtime: Runtime, intentional: bool) -> None:
        if not runtime.pid or not process.alive(runtime.pid):
            runtime.pid = None
            if runtime.phase != "stopping":
                runtime.phase = "stopped"
            return
        runtime.intentional = intentional
        runtime.phase = "stopping"
        runtime.stop_deadline = time.monotonic() + runtime.spec.stop_grace_seconds
        process.signal_group(runtime.pid, signal.SIGTERM)
        self.events.emit(
            "stopping",
            service=runtime.spec.name,
            pid=runtime.pid,
            grace_seconds=runtime.spec.stop_grace_seconds,
        )

    def _command(self, runtime: Runtime) -> list[str]:
        if runtime.spec.command_env:
            override = os.environ.get(runtime.spec.command_env, "").strip()
            if override:
                return [self._sub(part) for part in shlex.split(override)]
        return list(runtime.spec.command)

    def _sub(self, value: str) -> str:
        for key, repl in self.mapping.items():
            value = value.replace("{" + key + "}", repl)
        return value

    def _child_env(self, runtime: Runtime, nonce: str) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in runtime.spec.env.items():
            env[key] = value
        env["AVRONE_SVC_ID"] = f"{runtime.spec.name}:{nonce}"
        env["AVRONE_SVC_MANAGED"] = "1"
        return env

    def _start_reader(self, runtime: Runtime, child: process.Child) -> None:
        stream = child.popen.stdout

        def pump() -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if runtime.log is not None:
                        runtime.log.write(chunk)
                    with runtime.tail_lock:
                        runtime.tail.extend(chunk)
                        if len(runtime.tail) > 16384:
                            del runtime.tail[: len(runtime.tail) - 16384]
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        thread = threading.Thread(target=pump, name=f"log-{runtime.spec.name}", daemon=True)
        thread.start()
        runtime.reader = thread

    def _open_log(self, runtime: Runtime) -> RotatingLog:
        path = self.state_dir / "logs" / f"{runtime.spec.name}.log"
        return RotatingLog(path, runtime.spec.log_max_bytes, runtime.spec.log_backups)

    def _mark_cycles(self) -> None:
        cycles = find_cycles([runtime.spec for runtime in self.runtime.values() if not runtime.removed])
        involved: dict[str, str] = {}
        for cycle in cycles:
            detail = "dependency cycle: " + " -> ".join(cycle)
            for name in cycle[:-1]:
                involved[name] = detail
        for name, detail in involved.items():
            runtime = self.runtime.get(name)
            if runtime is None:
                continue
            if runtime.reason == detail and runtime.phase == "failed":
                continue
            runtime.phase = "failed"
            runtime.desired = "stopped"
            runtime.reason = detail
            if runtime.pid:
                self._begin_stop(runtime, intentional=True)
            self.events.emit("anomaly", service=name, kind="dependency_cycle", detail=detail)
            self._persist_locked(runtime)

    def _restore(self, runtime: Runtime) -> None:
        if "dependency cycle" in runtime.reason:
            return
        saved = self._state.service(runtime.spec.name)
        if "enabled_override" in saved and saved.get("enabled_override") is not None:
            runtime.enabled_override = bool(saved["enabled_override"])
        runtime.restarts = int(saved.get("restarts") or 0)
        runtime.attempt = int(saved.get("attempt") or 0)
        runtime.restart_epochs = [float(item) for item in saved.get("restart_epochs") or []]
        pid = saved.get("pid")
        nonce = saved.get("nonce") or ""
        if pid and process.matches_identity(int(pid), runtime.spec.name, nonce):
            if saved.get("desired") == "stopped" or not runtime.enabled:
                process.signal_group(int(pid), signal.SIGTERM)
                self.events.emit("orphan_cleanup", service=runtime.spec.name, pid=int(pid))
            else:
                runtime.pid = int(pid)
                runtime.nonce = nonce
                runtime.adopted = True
                runtime.desired = "running"
                runtime.phase = "starting"
                runtime.started_at = str(saved.get("started_at") or "")
                runtime.started_mono = time.monotonic()
                runtime.next_health = time.monotonic()
                self._by_pid[runtime.pid] = runtime
                self._tracked.add(runtime.pid)
                self.events.emit("adopt", service=runtime.spec.name, pid=runtime.pid)
                self._persist_locked(runtime)
                return
        until = float(saved.get("circuit_until_epoch") or 0)
        if saved.get("phase") == "circuit_open" and until > time.time():
            runtime.phase = "circuit_open"
            runtime.circuit_until_epoch = until
            runtime.circuit_until = time.monotonic() + (until - time.time())
            runtime.reason = str(saved.get("reason") or "circuit open")
        if runtime.enabled and saved.get("desired", "running") != "stopped":
            runtime.desired = "running"
        else:
            runtime.desired = "stopped"
        self._persist_locked(runtime)

    def _cleanup_removed(self) -> None:
        known = set(self.runtime)
        for name, saved in self._state.services().items():
            if name in known:
                continue
            pid = saved.get("pid")
            nonce = saved.get("nonce") or ""
            if pid and process.matches_identity(int(pid), name, nonce):
                process.signal_group(int(pid), signal.SIGTERM)
                self.events.emit("orphan_cleanup", service=name, pid=int(pid), detail="removed from config")

    def _persist_locked(self, runtime: Runtime) -> None:
        self._state.update_service(
            runtime.spec.name,
            desired=runtime.desired,
            enabled_override=runtime.enabled_override,
            phase=runtime.phase,
            reason=runtime.reason,
            pid=runtime.pid,
            nonce=runtime.nonce,
            command=list(runtime.spec.command),
            started_at=runtime.started_at,
            exit_code=runtime.exit_code,
            restarts=runtime.restarts,
            attempt=runtime.attempt,
            restart_epochs=list(runtime.restart_epochs),
            circuit_until_epoch=runtime.circuit_until_epoch,
        )

    def _require(self, name: str) -> Runtime:
        runtime = self.runtime.get(name)
        if runtime is None or runtime.removed:
            raise SupervisorError(f"unknown service {name}")
        return runtime

    def _start_api(self) -> None:
        from .api import serve
        self._api, self.api_port = serve(self, self.api_host, self.api_port, self._token)
        payload = {
            "host": self.api_host,
            "port": self.api_port,
            "pid": os.getpid(),
            "token_file": str(self.state_dir / "token"),
        }
        import json
        (self.state_dir / "api.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(self.state_dir / "api.json", 0o644)
        sys.stdout.write(f"avrone-svc listening http://{self.api_host}:{self.api_port}\n")
        sys.stdout.flush()

    def _install_signals(self) -> None:
        def on_hup(signum, frame):
            self._reload_flag = True
            self._poke()

        def on_stop(signum, frame):
            self._stop = True
            self._poke()

        signal.signal(signal.SIGHUP, on_hup)
        signal.signal(signal.SIGTERM, on_stop)
        signal.signal(signal.SIGINT, on_stop)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    def _poke(self) -> None:
        try:
            os.write(self._wake_w, b"\x01")
        except OSError:
            pass

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._wake_r, 256)
            except BlockingIOError:
                return
            if not chunk:
                return

    def _sleep(self) -> None:
        self._selector.select(timeout=self.tick)
        self._drain()


def find_repo(config: Path) -> Path:
    env = os.environ.get("AVRONE_REPO") or os.environ.get("AVRONE_WORKSPACE")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    start = config if config.is_dir() else config.parent
    for candidate in [start, *start.parents]:
        manifest = candidate / "pyproject.toml"
        if not manifest.is_file():
            continue
        try:
            head = manifest.read_text(encoding="utf-8", errors="replace")[:800]
        except OSError:
            continue
        if 'name = "avrone"' in head or "name = 'avrone'" in head:
            return candidate
    return Path.cwd()


def load_or_create_token(path: Path) -> str:
    env = os.environ.get("AVRONE_SVC_TOKEN", "").strip()
    if env:
        path.write_text(env + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return env
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            os.chmod(path, 0o600)
            return token
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def bundle_dir() -> Path:
    return Path(__file__).resolve().parent / "bundle"


def default_state_dir() -> Path:
    env = os.environ.get("AVRONE_SVC_STATE", "").strip()
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "avrone-svc"
